"""智能体检索层 / 决策日志 / LLM 客户端 / 提示词组装的单元测试（M4-2）。

验收口径（V4.5、V4.7 的单元层）：
- 决策日志每行一个 JSON，可被 `json.loads` 解析，按天滚动，写失败不抛；
- `AgentLLM` 复用 `llm` 配置、失败原样重试 1 次、连续失败抛 `AgentLLMError`；
- L2 召回按 2-gram 相似度排序、`has_audio` 标注正确、最近说过的素材在窗口内不再召回；
- system prompt 把五段拼全，且空段不留空壳。
"""
import asyncio
import json
from datetime import datetime

import pytest

from hub.agent.llm import AgentLLM, AgentLLMError, DECISION_MAX_TOKENS
from hub.agent.log import DecisionLogger
from hub.agent.memory import SessionMemory
from hub.agent.prompt import (
    build_agent_system_prompt,
    build_agent_user_message,
    describe_event,
)
from hub.agent.retriever import L2Retriever, Retriever
from hub.events import LiveEvent


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# ---------------------------------------------------------------- 决策日志


def test_decision_log_writes_jsonl_and_reads_back(tmp_path):
    """V4.5：每行一个 JSON，可被 json.loads 解析。"""
    log = DecisionLogger(log_dir=tmp_path, clock=Clock(1758000000.0))
    log.write({"type": "chat", "user": "阿强", "should_reply": True,
               "reply_text": "在的兄弟"})
    log.write({"type": "gift", "user": "小明", "should_reply": False})
    path = log.path()
    assert path is not None and path.name.startswith("agent-")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    records = [json.loads(line) for line in lines]
    assert records[0]["reply_text"] == "在的兄弟"
    assert records[0]["ts"].startswith("2025-09-16")
    assert [r["user"] for r in log.read_recent()] == ["阿强", "小明"]


def test_decision_log_disabled_writes_nothing(tmp_path):
    log = DecisionLogger(log_dir=tmp_path, enabled=False)
    log.write({"type": "chat"})
    assert log.path() is None
    assert log.read_recent() == []


def test_decision_log_rolls_over_by_day(tmp_path):
    clock = Clock(1758000000.0)
    log = DecisionLogger(log_dir=tmp_path, clock=clock)
    log.write({"n": 1})
    first = log.path()
    clock.advance(86400)
    log.write({"n": 2})
    assert log.path() != first
    assert first.exists() and log.path().exists()


def test_decision_log_unwritable_dir_does_not_raise(tmp_path):
    """日志是旁路观测：目录建不出来也只能静默降级，不能反噬决策链路。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file", encoding="utf-8")
    log = DecisionLogger(log_dir=blocker, clock=Clock())
    assert log.path() is None
    log.write({"type": "chat"})          # 不抛


def test_decision_log_bad_lines_are_skipped(tmp_path):
    clock = Clock(1758000000.0)
    log = DecisionLogger(log_dir=tmp_path, clock=clock)
    log.write({"n": 1})
    log.path().write_text('{"n": 1}\nnot json\n', encoding="utf-8")
    assert [r["n"] for r in log.read_recent()] == [1]


def test_decision_log_set_enabled_hot_switch(tmp_path):
    log = DecisionLogger(log_dir=tmp_path, clock=Clock(1758000000.0))
    log.write({"n": 1})
    log.set_enabled(False)
    log.write({"n": 2})
    log.set_enabled(True)
    log.write({"n": 3})
    assert [r["n"] for r in log.read_recent()] == [1, 3]


# ---------------------------------------------------------------- LLM 客户端


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeClient:
    def __init__(self, behaviors):
        self._behaviors = behaviors
        self.captured = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        self.captured.append({"url": url, "json": json, "headers": headers})
        behavior = self._behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


def ok(text='{"should_reply": true, "reply_text": "在的兄弟"}'):
    return FakeResponse(payload={"choices": [{"message": {"content": text}}]})


def make_llm(behaviors, **cfg_over):
    llm_cfg = {"base_url": "https://api.example.com/v1", "api_key": "sk-test",
               "model": "test-model", "temperature": 0.8, "max_tokens": 120,
               "timeout_seconds": 8}
    llm_cfg.update(cfg_over)
    return AgentLLM(config={"llm": llm_cfg}, client_factory=lambda: FakeClient(behaviors))


def test_agent_llm_returns_raw_text_and_uses_llm_config():
    client = FakeClient([ok()])
    llm = AgentLLM(config={"llm": {"base_url": "https://api.example.com/v1/",
                                   "api_key": "sk-test", "model": "deepseek-v4-flash"}},
                    client_factory=lambda: client)
    text = asyncio.run(llm.ask("system 提示", "user 消息"))
    assert text.startswith("{")
    sent = client.captured[0]
    assert sent["url"] == "https://api.example.com/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer sk-test"
    assert sent["json"]["model"] == "deepseek-v4-flash"
    assert sent["json"]["messages"][0]["role"] == "system"


def test_agent_llm_max_tokens_is_bigger_than_rules_default():
    """决策 JSON 装不进 llm.max_tokens 的 120，必须单独给额度。"""
    assert DECISION_MAX_TOKENS > 120
    client = FakeClient([ok()])
    llm = AgentLLM(config={"llm": {"base_url": "u", "api_key": "k", "model": "m"}},
                   client_factory=lambda: client)
    asyncio.run(llm.ask("s", "u"))
    assert client.captured[0]["json"]["max_tokens"] == DECISION_MAX_TOKENS


def test_agent_llm_retries_once_then_succeeds():
    llm = make_llm([RuntimeError("boom"), ok()])
    assert "should_reply" in asyncio.run(llm.ask("s", "u"))


def test_agent_llm_raises_after_two_failures():
    for behavior in (FakeResponse(status_code=500),
                     FakeResponse(payload={"error": "x"}),
                     FakeResponse(payload={"choices": []}),
                     FakeResponse(payload={"choices": [{"message": {"content": "  "}}]}),
                     RuntimeError("boom")):
        llm = make_llm([behavior, type(behavior)() if isinstance(behavior, Exception)
                        else FakeResponse(status_code=500)])
        with pytest.raises(AgentLLMError):
            asyncio.run(llm.ask("s", "u"))


def test_agent_llm_requires_configuration():
    llm = AgentLLM(config={"llm": {"base_url": "", "api_key": "", "model": ""}})
    assert llm.configured is False
    with pytest.raises(AgentLLMError):
        asyncio.run(llm.ask("s", "u"))


def test_agent_llm_timeout_has_a_floor_for_bigger_prompts():
    llm = AgentLLM(config={"llm": {"base_url": "u", "api_key": "k", "model": "m",
                                   "timeout_seconds": 3}})
    assert llm._timeout_s >= 12.0
    llm2 = AgentLLM(config={"llm": {"base_url": "u", "api_key": "k", "model": "m",
                                    "timeout_seconds": 30}})
    assert llm2._timeout_s == 30.0


# ------------------------------------------------- M6：function calling 入口

def tool_msg(tool_calls, content=""):
    return FakeResponse(payload={"choices": [{"message": {
        "content": content, "tool_calls": tool_calls}}]})


TOOLS = [{"type": "function", "function": {"name": "set_style",
                                           "parameters": {"type": "object",
                                                          "properties": {}}}}]


def test_ask_message_returns_raw_message_and_sends_tools():
    calls = [{"id": "c1", "type": "function",
              "function": {"name": "set_interval_min",
                           "arguments": '{"minutes": 15}'}}]
    client = FakeClient([tool_msg(calls)])
    llm = AgentLLM(config={"llm": {"base_url": "u", "api_key": "k", "model": "m"}},
                   client_factory=lambda: client)
    msg = asyncio.run(llm.ask_message("s", "u", tools=TOOLS))
    assert msg["tool_calls"][0]["function"]["name"] == "set_interval_min"
    sent = client.captured[0]["json"]
    assert sent["tools"] == TOOLS and sent["tool_choice"] == "auto"
    assert sent["messages"][0]["role"] == "system"


def test_ask_message_allows_empty_content_when_tools_given():
    """带 tools 时 content 为空是合法的（模型只回工具调用）——不该重试、不该报错。"""
    calls = [{"id": "c1", "type": "function",
              "function": {"name": "set_style", "arguments": "{}"}}]
    client = FakeClient([tool_msg(calls, content="")])
    llm = AgentLLM(config={"llm": {"base_url": "u", "api_key": "k", "model": "m"}},
                   client_factory=lambda: client)
    msg = asyncio.run(llm.ask_message("s", "u", tools=TOOLS))
    assert msg["tool_calls"] and not str(msg["content"]).strip()
    assert len(client.captured) == 1          # 没触发重试


def test_plain_ask_still_retries_on_empty_content():
    """不带 tools 的老口径不变：空响应算失败、重试一次（回归保护）。"""
    llm = make_llm([FakeResponse(payload={"choices": [{"message": {"content": "   "}}]}),
                    ok("{}")])
    assert asyncio.run(llm.ask("s", "u")) == "{}"


def test_message_not_object_is_treated_as_failure():
    llm = make_llm([FakeResponse(payload={"choices": [{"message": "不是对象"}]}),
                    FakeResponse(status_code=500)])
    with pytest.raises(AgentLLMError):
        asyncio.run(llm.ask("s", "u"))


# ---------------------------------------------------------------- L2 检索

PROFILE = {
    "samples_ai": [
        {"id": "s1", "enabled": True, "type_name": "问下载",
         "examples": ["这游戏怎么下载", "在哪下载啊"],
         "reply_text": "右下角小手柄点一下就能下载，进去挂着就行"},
        {"id": "s2", "enabled": False, "type_name": "问年龄",
         "examples": ["主播你多大了"], "reply_text": "这是秘密"},
        {"id": "s3", "enabled": True, "type_name": "空回复",
         "examples": ["随便"], "reply_text": ""},
    ],
    "topics_ai": [
        {"id": "t1", "enabled": True, "text": "这款游戏主打休闲挂机，上班摸鱼也能玩"},
        {"id": "t2", "enabled": False, "text": "被禁用的话题不该进索引"},
    ],
}


def make_retriever(clock=None, resolver=None, **kw):
    return L2Retriever(clock=clock or Clock(),
                       audio_resolver=resolver, **kw)


def test_retriever_interface_is_abstract():
    with pytest.raises(NotImplementedError):
        Retriever().retrieve("x")
    with pytest.raises(NotImplementedError):
        Retriever().index({}, [])


def test_index_skips_disabled_and_empty_entries():
    r = make_retriever()
    r.index(PROFILE)
    ids = {e["id"] for e in r._entries}
    assert ids == {"sample:s1", "topic:t1"}
    assert r.size == 2


def test_retrieve_ranks_by_similarity_and_returns_contract_fields():
    r = make_retriever(resolver=lambda item, emotion: (
        "/audio/a.wav" if item.get("id") == "s1" else ""))
    r.index(PROFILE)
    hits = r.retrieve("这游戏怎么下载", top_k=5)
    assert hits and hits[0]["id"] == "sample:s1"
    top = hits[0]
    assert set(top) == {"id", "kind", "match", "reply", "score", "has_audio"}
    assert top["kind"] == "sample"
    assert top["has_audio"] is True                      # V4.7
    assert all(("sample:s2" != h["id"]) for h in hits)   # 未启用不进候选


def test_retrieve_marks_has_audio_false_without_audio():
    r = make_retriever()          # 未注入 resolver → 一律无音频
    r.index(PROFILE)
    hits = r.retrieve("这游戏怎么下载")
    assert hits[0]["has_audio"] is False


def test_retrieve_returns_empty_for_blank_query_or_empty_index():
    r = make_retriever()
    r.index(PROFILE)
    assert r.retrieve("") == []
    assert r.retrieve("   ") == []
    empty = make_retriever()
    empty.index({})
    assert empty.retrieve("这游戏怎么下载") == []


def test_retrieve_respects_top_k():
    r = make_retriever()
    r.index(PROFILE)
    assert len(r.retrieve("这游戏怎么下载 休闲挂机 上班摸鱼", top_k=1)) == 1


def test_recently_spoken_material_is_not_recalled_within_window():
    """防复读：说出去的素材 600 s 内不再召回，窗口过期后回归。"""
    clock = Clock()
    r = make_retriever(clock=clock)
    r.index(PROFILE)
    reply = "右下角小手柄点一下就能下载，进去挂着就行"
    assert r.note_spoken(reply) == ["sample:s1"]
    assert all(h["id"] != "sample:s1" for h in r.retrieve("这游戏怎么下载"))
    clock.advance(601)
    assert any(h["id"] == "sample:s1" for h in r.retrieve("这游戏怎么下载"))


def test_index_rebuild_picks_up_profile_edits():
    r = make_retriever()
    r.index(PROFILE)
    assert r.size == 2
    edited = dict(PROFILE)
    edited["samples_ai"] = PROFILE["samples_ai"] + [
        {"id": "s9", "enabled": True, "examples": ["唱歌挺好听"],
         "reply_text": "谢谢夸奖，随便哼两句"}]
    r.index(edited)
    assert r.size == 3
    assert r.retrieve("唱歌挺好听")[0]["id"] == "sample:s9"


def test_recall_from_feedback_examples():
    """反馈回流样本（M6 落盘）也能参与召回，kind=example。"""
    r = make_retriever()
    r.index({}, examples=[{"id": "ex_1", "source": "adopted",
                           "event": {"type": "chat", "content": "这游戏卡不卡"},
                           "reply": "一点都不卡，手机老年机都能跑"}])
    hits = r.retrieve("这游戏卡不卡")
    assert hits[0]["kind"] == "example"
    assert hits[0]["id"] == "example:ex_1"


# ---------------------------------------------------------------- 提示词


def test_system_prompt_assembles_all_five_sections():
    prompt = build_agent_system_prompt(
        "你是主播仔仔鼠",
        recalls=[{"kind": "sample", "match": "怎么下载", "reply": "点小手柄",
                  "has_audio": True}],
        memory_brief={"events": ["[12:00:00] 弹幕 阿强：怎么下载"],
                      "spoken": ["欢迎新来的兄弟"], "notes": ["答过下载"]},
        emotions=["default", "happy"],
        max_reply_chars=25,
    )
    assert prompt.startswith("你是主播仔仔鼠")
    assert "【本轮素材召回】" in prompt and "有预录音频" in prompt
    assert "【本场直播记忆】" in prompt and "避免重复同一句话" in prompt
    assert "可用情绪档位：default, happy" in prompt
    assert "【输出要求】" in prompt
    assert "不超过 25 字" in prompt
    assert '"should_reply"' in prompt          # 契约示例


def test_system_prompt_omits_empty_sections():
    prompt = build_agent_system_prompt("底料", max_reply_chars=30)
    assert "【本轮素材召回】" not in prompt
    assert "【本场直播记忆】" not in prompt
    assert "【可用情绪档位】" in prompt
    assert "【输出要求】" in prompt
    assert "\n\n\n" not in prompt


def test_describe_event_covers_every_event_type():
    assert describe_event({"type": "chat", "user": "阿强", "content": "在吗"}) == "阿强：在吗"
    assert "另有 1 条相同弹幕" in describe_event(
        {"type": "chat", "user": "阿强", "content": "在吗", "merged_count": 2,
         "members": [("阿强", "在吗"), ("小李", "在吗")]})
    gift = describe_event({"type": "gift", "user": "小明", "gift_name": "小心心",
                           "gift_count": 5, "gift_value": 10, "tier": "mid"})
    assert gift == "小明 送出 小心心 ×5（价值 10），属于中额礼物"
    assert describe_event({"type": "follow", "user": "小红"}) == "小红 关注了直播间"
    assert describe_event({"type": "fansclub", "user": "小李", "content": "冲"}) == \
        "小李 加入了粉丝团，附言：冲"
    assert describe_event({"type": "fansclub", "user": "小李"}) == "小李 加入了粉丝团"
    assert describe_event({"type": "enter", "user": "小张"}) == "小张 进入了直播间"


def test_user_message_includes_event_and_instruction():
    msg = build_agent_user_message({"type": "gift", "user": "小明",
                                    "gift_name": "火箭", "gift_count": 1,
                                    "gift_value": 5000})
    assert msg.startswith("【当前事件】礼物")
    assert "小明 送出 火箭 ×1（价值 5000）" in msg
    assert "按输出要求给出 JSON" in msg


def test_memory_section_renders_real_brief_as_human_lines():
    """回归：记忆段必须吃 `SessionMemory.brief()` 的真实产物，而不是手搓的字符串列表。

    曾经的 bug：`brief()` 返回事件 **dict**，而 `_memory_section` 是纯文本装配器
    （`f"· {行}"`），于是提示词里出现
    `· {'type': 'chat', 'user': '观众甲', 'ts': 1790049318.18}` 这样的 Python repr——
    既烧 token 又难读。当时提示词单测传的是"理想形状"的字符串列表，所以全绿。
    这里改成从真的 SessionMemory 取 brief，形状对不上就会红。
    """
    mem = SessionMemory()
    mem.add_event(LiveEvent(type="chat", user="观众甲", content="主播这是什么游戏",
                            ts=1700000001.0))
    mem.add_event(LiveEvent(type="gift", user="小明", gift_name="小心心",
                            gift_count=5, gift_value=10, ts=1700000002.0))

    prompt = build_agent_system_prompt("底料", memory_brief=mem.brief())

    assert "【本场直播记忆】" in prompt
    # 事件行按本地时区渲染，断言里现算时间戳（不写死，避免依赖运行机器的时区）
    chat_at = datetime.fromtimestamp(1700000001.0).strftime("%H:%M:%S")
    gift_at = datetime.fromtimestamp(1700000002.0).strftime("%H:%M:%S")
    assert f"· [{chat_at}] 弹幕 观众甲：主播这是什么游戏" in prompt
    assert f"· [{gift_at}] 礼物 小明 送出 小心心 ×5（价值 10）" in prompt
    # 关键：提示词里不得出现事件 dict 的 repr 残片
    for noise in ("'type':", "'gift_value':", "{'type'", "1700000001.0"):
        assert noise not in prompt, noise

