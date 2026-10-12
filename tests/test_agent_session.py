"""对话式调参的单元测试（M6-5）。

验收口径：

- **白名单**：未知工具、越界参数、坏 JSON 一律拒；模型说得出名字才改得动；
- **落点正确**：人设/字数/话题/话术/游戏知识进存档，口播间隔两处都写
  （存档 `schedule.interval_s` 是热的，`config.yaml` 是同步引导值）；
- **写前快照 + 撤销**：撤销走同一套 setter，config 行尾注释完整，存档只回写被动过的分区；
- **热生效**：applier 收到变更；**接缝实测**——调完 `max_reply_chars` / 间隔后，
  真 `ScriptRuntime` 读到的就是新值（不喂理想形状，喂真存档）。
"""
import json

import pytest

from hub.agent.session import (
    INTERVAL_MIN_RANGE,
    MAX_SNAPSHOTS,
    REPLY_CHARS_RANGE,
    SessionError,
    SessionTuner,
    tool_specs,
)
from hub.scriptstore import ScriptStore
from hub.selector import ScriptRuntime


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


class Clock:
    def __init__(self, start=1758000000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def tool_call(name, arguments, call_id="call_1"):
    """OpenAI 形状的 tool_call：arguments 是**字符串**形式的 JSON（别喂 dict）。"""
    if isinstance(arguments, dict):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


class FakeLLM:
    """替身只实现 `ask_message`（会话层唯一用到的入口）。"""

    def __init__(self, replies, configured=True):
        self._replies = list(replies)
        self.configured = configured
        self.calls = []

    async def ask_message(self, system, user, tools=None, tool_choice="auto",
                          max_tokens=None):
        self.calls.append({"system": system, "user": user, "tools": tools,
                           "tool_choice": tool_choice})
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        # 只给一条 tool_call 时允许直接传它（省掉 msg(tool_calls=[...]) 的壳），
        # 但**必须在这里补壳**：生产里模型返回的是 message，`tool_calls` 挂在它下面，
        # 少了这层壳会话层就按"纯对话"处理——曾因此让一批用例假绿/假红。
        if isinstance(item, dict) and "function" in item:
            item = msg(tool_calls=[item])
        return item


def msg(tool_calls=None, content="", **extra):
    m = {"role": "assistant", "content": content}
    if tool_calls is not None:
        m["tool_calls"] = tool_calls
    m.update(extra)
    return m


@pytest.fixture
def config_yaml(tmp_path):
    """一份带注释的最小 config.yaml（注释必须在写回后仍在）。"""
    p = tmp_path / "config.yaml"
    p.write_text(
        "llm:\n  base_url: \"u\"\n\nschedule:\n  enabled: true\n"
        "  interval_min: 7          # 实际取 7±1 分钟随机\n  pool:\n    - \"老话术\"\n",
        encoding="utf-8")
    return p


@pytest.fixture
def store(tmp_path):
    s = ScriptStore(root=tmp_path / "profiles")
    pid = s.create_profile("调参测试")["id"]
    data = s.get_profile(pid)
    data["knowledge"].update({"style": "口语化、热情、短句",
                              "max_reply_chars": 40,
                              "game_name": "《示例游戏》",
                              "game_intro": "MMORPG 手游",
                              "download_hint": "点小手柄下载"})
    data["schedule"] = {"enabled": True, "interval_s": 420, "jitter_s": 60}
    s.save_profile(pid, data)
    return s


def make_tuner(store, replies, config_yaml=None, cfg=None, applier=None):
    pid = store.list_profiles()[0]["id"]
    llm = FakeLLM(replies)
    tuner = SessionTuner(llm=llm, store=store, pid_provider=lambda: pid,
                         config_path=config_yaml, applier=applier,
                         cfg=cfg if cfg is not None else {"schedule": {"interval_min": 7}},
                         clock=Clock())
    return tuner, llm, pid


def run(coro):
    import asyncio
    return asyncio.run(coro)


# ---------------------------------------------------------------- 可用性 / 状态

def test_tools_are_whitelisted_and_documented():
    names = {t["function"]["name"] for t in tool_specs()}
    assert names == {"set_style", "set_max_reply_chars", "set_interval_min",
                     "add_topic", "add_sample", "set_game_knowledge"}
    for t in tool_specs():
        assert t["type"] == "function"
        assert t["function"]["description"]
        assert t["function"]["parameters"]["type"] == "object"


def test_unavailable_without_llm_key(store, config_yaml):
    tuner, _, _ = make_tuner(store, [msg(content="x")], config_yaml)
    tuner._llm.configured = False
    assert tuner.available is False
    state = tuner.state()
    assert state["available"] is False and "LLM Key" in state["reason"]
    out = run(tuner.send("口播太频繁了"))
    assert out["ok"] is False and out["changes"] == []
    # 关键：不可用时连模型都不该调（不能烧额度）
    assert tuner._llm.calls == []


def test_state_reports_current_values(store, config_yaml):
    tuner, _, _ = make_tuner(store, [msg(content="x")], config_yaml)
    cur = tuner.state()["current"]
    assert cur["style"] == "口语化、热情、短句"
    assert cur["max_reply_chars"] == 40
    assert cur["interval_min"] == 7          # 420 秒 → 7 分钟
    assert cur["game_name"] == "《示例游戏》"


def test_no_profile_means_unavailable(tmp_path, config_yaml):
    empty = ScriptStore(root=tmp_path / "empty")
    tuner = SessionTuner(llm=FakeLLM([msg(content="x")]), store=empty,
                         pid_provider=lambda: "", config_path=config_yaml)
    assert tuner.available is False
    assert "存档" in tuner.state()["reason"]


# ---------------------------------------------------------------- 纯对话 / 空输入

def test_plain_chat_does_not_write_anything(store, config_yaml, monkeypatch):
    tuner, _, pid = make_tuner(store, [msg(content="我能改语气、字数、口播间隔。")])
    before = store.get_profile(pid)
    out = run(tuner.send("你能改什么"))
    assert out["ok"] and out["changes"] == [] and out["snapshot_id"] == ""
    assert store.get_profile(pid) == before
    assert tuner.snapshots() == []


def test_empty_request_is_rejected_without_calling_llm(store, config_yaml):
    tuner, llm, _ = make_tuner(store, [msg(content="x")])
    out = run(tuner.send("   "))
    assert out["ok"] is False and llm.calls == []


def test_llm_failure_reports_error(store, config_yaml):
    tuner, _, _ = make_tuner(store, [RuntimeError("连接超时")])
    out = run(tuner.send("口播太频繁了"))
    assert out["ok"] is False and "连接超时" in out["reply"]
    assert tuner.snapshots() == []


# ---------------------------------------------------------------- 单字段写入

def test_set_interval_writes_both_places_and_echoes(store, config_yaml):
    applied = []
    tuner, _, pid = make_tuner(store, [tool_call("set_interval_min", {"minutes": 15})],
                               config_yaml, applier=lambda ch: applied.extend(ch) or ["ok"])
    out = run(tuner.send("口播太频繁了"))
    assert out["ok"] and out["errors"] == []
    assert out["changes"][0]["field"] == "口播间隔"
    assert out["changes"][0]["old"] == "7 分钟" and out["changes"][0]["new"] == "15 分钟"
    # ① config.yaml：值变了、注释还在、pool 没动
    text = config_yaml.read_text(encoding="utf-8")
    assert "interval_min: 15" in text and "# 实际取 7±1 分钟随机" in text
    assert "    - \"老话术\"" in text
    # ② 存档：interval_s 是**真正生效**的那份
    assert store.get_profile(pid)["schedule"]["interval_s"] == 900
    # ③ 热生效回调被调到了
    assert applied and applied[0]["path"] == "schedule.interval_s"
    # ④ 回显「字段：旧值 → 新值」
    assert "7 分钟 → 15 分钟" in out["reply"]


def test_set_max_reply_chars_goes_to_profile(store, config_yaml):
    tuner, _, pid = make_tuner(store, [tool_call("set_max_reply_chars", {"chars": 28})])
    out = run(tuner.send("回复太长了"))
    assert out["changes"][0]["new"] == "28 字"
    assert store.get_profile(pid)["knowledge"]["max_reply_chars"] == 28


def test_set_style_goes_to_profile(store, config_yaml):
    tuner, _, pid = make_tuner(store, [tool_call("set_style", {"style": "稳重、少用网络词"})])
    run(tuner.send("别那么咋咋呼呼的"))
    assert store.get_profile(pid)["knowledge"]["style"] == "稳重、少用网络词"


def test_add_topic_appends_with_id(store, config_yaml):
    tuner, _, pid = make_tuner(store, [tool_call("add_topic", {
        "title": "新区开荒", "text": "今晚新区开荒，兄弟们一起来"})])
    out = run(tuner.send("加个话题：今晚新区开荒"))
    topics = store.get_profile(pid)["topics_ai"]
    assert len(topics) == 1 and topics[0]["id"].startswith("t_")
    assert topics[0]["enabled"] is True and topics[0]["text"] == "今晚新区开荒，兄弟们一起来"
    assert "条" in out["changes"][0]["old"] and "新增" in out["changes"][0]["new"]


def test_add_topic_without_title_uses_text_head(store, config_yaml):
    tuner, _, pid = make_tuner(store, [tool_call("add_topic", {"text": "今晚新区开荒兄弟们一起来"})])
    run(tuner.send("加个话题"))
    assert store.get_profile(pid)["topics_ai"][0]["title"]


def test_add_sample_splits_examples(store, config_yaml):
    tuner, _, pid = make_tuner(store, [tool_call("add_sample", {
        "examples": "怎么下载 / 在哪下载啊", "reply_text": "点右下角小手柄就行"})])
    run(tuner.send("加条话术"))
    s = store.get_profile(pid)["samples_ai"][0]
    assert s["examples"] == ["怎么下载", "在哪下载啊"]
    assert s["reply_text"] == "点右下角小手柄就行" and s["enabled"] is True


def test_set_game_knowledge_only_touches_given_keys(store, config_yaml):
    tuner, _, pid = make_tuner(store, [tool_call("set_game_knowledge", {
        "highlights": ["八大职业", "百人攻城战"]})])
    out = run(tuner.send("卖点改成这两条"))
    k = store.get_profile(pid)["knowledge"]
    assert k["highlights"] == ["八大职业", "百人攻城战"]
    assert k["game_intro"] == "MMORPG 手游"          # 没传的不动
    assert "卖点" in out["changes"][0]["field"]


# ---------------------------------------------------------------- 白名单 / 边界

@pytest.mark.parametrize("call,needle", [
    (tool_call("rm_rf", {"path": "/"}), "未知工具"),
    (tool_call("set_max_reply_chars", {"chars": 999}), "20 ~ 60"),
    (tool_call("set_interval_min", {"minutes": 0}), "1 ~ 60"),
    (tool_call("set_interval_min", {"minutes": "十分钟"}), "整数"),
    (tool_call("set_style", {"style": "  "}), "不能为空"),
    (tool_call("add_sample", {"reply_text": " "}), "不能为空"),
    (tool_call("set_game_knowledge", {}), "没有可改的项"),
    (tool_call("add_topic", "{不是 JSON"), "不是合法 JSON"),
])
def test_bad_calls_are_rejected_and_nothing_is_written(store, config_yaml, call, needle):
    tuner, _, pid = make_tuner(store, [msg(tool_calls=[call], content="")])
    before = store.get_profile(pid)
    out = run(tuner.send("随便改改"))
    assert out["ok"] is False
    assert any(needle in e for e in out["errors"]), out["errors"]
    assert store.get_profile(pid) == before
    assert tuner.snapshots() == []
    assert config_yaml.read_text(encoding="utf-8").count("interval_min: 7") == 1


def test_one_bad_call_does_not_drop_the_others(store, config_yaml):
    """模型常一次纠多个字段：一项越界不该把其余合理改动一起丢掉。"""
    tuner, _, pid = make_tuner(store, [msg(tool_calls=[
        tool_call("set_max_reply_chars", {"chars": 30}, "c1"),
        tool_call("set_interval_min", {"minutes": 999}, "c2")], content="")])
    out = run(tuner.send("回复短点，间隔拉长"))
    assert out["ok"] and len(out["changes"]) == 1
    assert out["changes"][0]["field"] == "回复字数上限"
    assert store.get_profile(pid)["knowledge"]["max_reply_chars"] == 30
    assert out["errors"] and "1 ~ 60" in out["errors"][0]


def test_multiple_calls_share_one_snapshot(store, config_yaml):
    tuner, _, _ = make_tuner(store, [msg(tool_calls=[
        tool_call("set_max_reply_chars", {"chars": 25}, "c1"),
        tool_call("set_interval_min", {"minutes": 20}, "c2")], content="都调一下")])
    out = run(tuner.send("回复短点、间隔长点"))
    assert len(out["changes"]) == 2
    assert len(tuner.snapshots()) == 1
    assert len(tuner.snapshots()[0]["changes"]) == 2


def test_same_value_is_noop_without_snapshot(store, config_yaml):
    tuner, _, _ = make_tuner(store, [tool_call("set_max_reply_chars", {"chars": 40})])
    out = run(tuner.send("回复字数改成40"))
    assert out["ok"] and out["changes"] == [] and out["snapshot_id"] == ""
    assert tuner.snapshots() == []


def test_calls_are_capped(store, config_yaml):
    calls = [tool_call("set_max_reply_chars", {"chars": 21 + i}, "c%d" % i) for i in range(8)]
    tuner, _, _ = make_tuner(store, [msg(tool_calls=calls, content="")])
    out = run(tuner.send("一顿乱改"))
    assert len(out["changes"]) <= 4


def test_prompt_carries_current_values_and_request(store, config_yaml):
    tuner, llm, _ = make_tuner(store, [msg(content="好")])
    run(tuner.send("回复短一点"))
    sent = llm.calls[0]
    assert "回复字数上限：40 字" in sent["user"] and "口播间隔：7 分钟" in sent["user"]
    assert "【主播说】回复短一点" in sent["user"]
    assert sent["tools"] and sent["tools"][0]["function"]["name"] == "set_style"


# ---------------------------------------------------------------- 快照与撤销

def test_undo_restores_config_and_profile(store, config_yaml):
    tuner, _, pid = make_tuner(store, [msg(tool_calls=[
        tool_call("set_max_reply_chars", {"chars": 25}, "c1"),
        tool_call("set_interval_min", {"minutes": 20}, "c2")], content="")])
    run(tuner.send("回复短点、间隔长点"))
    assert store.get_profile(pid)["knowledge"]["max_reply_chars"] == 25

    out = tuner.undo()
    assert out["ok"] is True
    k = store.get_profile(pid)["knowledge"]
    assert k["max_reply_chars"] == 40
    assert store.get_profile(pid)["schedule"]["interval_s"] == 420
    text = config_yaml.read_text(encoding="utf-8")
    assert "interval_min: 7" in text and "# 实际取 7±1 分钟随机" in text


def test_undo_restores_style_without_touching_other_sections(store, config_yaml):
    tuner, _, pid = make_tuner(store, [tool_call("set_style", {"style": "新风格"})])
    run(tuner.send("换个风格"))
    # 撤销前主播在别处又存了一份存档（话题被改了）——撤销不能把它冲掉
    later = store.get_profile(pid)
    later["topics_ai"].append({"id": "t_zz", "title": "别处加的", "text": "别处加的",
                               "audios": {}, "enabled": True})
    store.save_profile(pid, later)

    tuner.undo()
    after = store.get_profile(pid)
    assert after["knowledge"]["style"] == "口语化、热情、短句"
    assert [t["id"] for t in after["topics_ai"]] == ["t_zz"]


def test_undo_topic_removes_appended_item(store, config_yaml):
    tuner, _, pid = make_tuner(store, [tool_call("add_topic", {"title": "T", "text": "正文"})])
    run(tuner.send("加话题"))
    assert len(store.get_profile(pid)["topics_ai"]) == 1
    tuner.undo()
    assert store.get_profile(pid)["topics_ai"] == []


def test_undo_consumes_snapshot(store, config_yaml):
    tuner, _, _ = make_tuner(store, [tool_call("set_max_reply_chars", {"chars": 30})])
    run(tuner.send("短点"))
    assert len(tuner.snapshots()) == 1
    tuner.undo()
    assert tuner.snapshots() == []
    again = tuner.undo()
    assert again["ok"] is False and "没有可撤销" in again["message"]


def test_undo_by_id_and_unknown_id(store, config_yaml):
    tuner, _, _ = make_tuner(store, [
        tool_call("set_max_reply_chars", {"chars": 30}, "c1"),
        tool_call("set_max_reply_chars", {"chars": 50}, "c2")])
    first = run(tuner.send("短点"))["snapshot_id"]
    run(tuner.send("长点"))
    assert len(tuner.snapshots()) == 2
    assert tuner.undo(first)["ok"] is True
    assert [s["id"] for s in tuner.snapshots()] != [first]
    assert tuner.undo("ss_nope")["ok"] is False


def test_snapshot_file_is_capped(store, config_yaml):
    replies = [tool_call("set_max_reply_chars", {"chars": 21 + i}, "c%d" % i)
               for i in range(MAX_SNAPSHOTS + 5)]
    tuner, _, _ = make_tuner(store, replies)
    for _ in range(MAX_SNAPSHOTS + 5):
        old = tuner.current()["max_reply_chars"]
        tuner._llm._replies.insert(0, tool_call(
            "set_max_reply_chars", {"chars": 21 if old != 21 else 22}))
        run(tuner.send("改一下"))
    assert len(tuner.snapshots()) == MAX_SNAPSHOTS


def test_snapshot_file_records_request_and_changes(store, config_yaml):
    tuner, _, _ = make_tuner(store, [tool_call("set_interval_min", {"minutes": 9})])
    run(tuner.send("口播间隔改成9分钟"))
    snap = tuner.snapshots()[0]
    assert snap["request"] == "口播间隔改成9分钟"
    assert snap["changes"][0]["old"] == "7 分钟" and snap["changes"][0]["new"] == "9 分钟"
    assert snap["restore"]["config"]["schedule.interval_min"] == 7
    # 快照真落盘了（不是只在内存）
    assert tuner._snapshot_path.exists()


def test_broken_snapshot_file_does_not_crash(store, config_yaml):
    tuner, _, _ = make_tuner(store, [msg(content="x")])
    tuner._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    tuner._snapshot_path.write_text("{ 坏 JSON", encoding="utf-8")
    assert tuner.snapshots() == []
    assert tuner.state()["snapshots"] == []


def test_applier_failure_does_not_undo_the_write(store, config_yaml):
    def boom(changes):
        raise RuntimeError("调度器不在")

    tuner, _, pid = make_tuner(store, [tool_call("set_max_reply_chars", {"chars": 33})],
                               config_yaml, applier=boom)
    out = run(tuner.send("短点"))
    assert out["ok"] is True
    assert store.get_profile(pid)["knowledge"]["max_reply_chars"] == 33


# ---------------------------------------------------------------- 接缝：真 ScriptRuntime 现读

def test_interval_change_is_visible_to_real_scheduler_config(store, config_yaml):
    """接缝实测：写进存档后，真 `ScriptRuntime.schedule()` 给出的 interval_s 就是新值。

    这是 V6.5「热生效」的可测部分——main 的 applier 就是走 reload + _apply_script_runtime，
    本用例只验到"运行时读得到新值"这一层（调度器真跑由端到端那层覆盖）。
    """
    tuner, _, pid = make_tuner(store, [tool_call("set_interval_min", {"minutes": 2})])
    run(tuner.send("口播太冷清了，勤点"))
    rt = ScriptRuntime(store, {"script": {"active_profile": pid}})
    rt.reload()
    assert rt.schedule()["interval_s"] == 120


def test_max_reply_chars_seam_into_selector(store, config_yaml):
    tuner, _, pid = make_tuner(store, [tool_call("set_max_reply_chars", {"chars": 22})])
    run(tuner.send("短一点"))
    rt = ScriptRuntime(store, {"script": {"active_profile": pid}})
    rt.reload()
    assert rt.max_reply_chars() == 22          # AgentBrain 每次决策现读的就是它


def test_style_change_seam_into_system_prompt(store, config_yaml):
    tuner, _, pid = make_tuner(store, [tool_call("set_style", {"style": "稳重、少用网络词"})])
    run(tuner.send("稳重点"))
    rt = ScriptRuntime(store, {"script": {"active_profile": pid}})
    rt.reload()
    assert "稳重、少用网络词" in rt.build_system_prompt({})


def test_added_topic_seam_into_rotation_pool(store, config_yaml):
    """新话题要能进轮播池——但池子只收「有当前音色音频且未过期」的条目，
    没音频时靠 skipped 日志提示（与话术管理页一键补齐一致），这里只验进得去。"""
    tuner, _, pid = make_tuner(store, [tool_call("add_topic", {"title": "新区", "text": "新区开荒"})])
    run(tuner.send("加话题"))
    rt = ScriptRuntime(store, {"script": {"active_profile": pid}})
    rt.reload()
    assert [t["text"] for t in rt.profile["topics_ai"]] == ["新区开荒"]
    assert rt.schedule()["items"] == []        # 无音频 → 不供料（不是 bug，是靠音频门控）
