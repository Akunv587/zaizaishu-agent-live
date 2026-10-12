"""试玩调试与决策透视聚合的单元测试（M5-1 / M5-2，DESIGN §7.2②④）。

验收口径：
- V5.3 试玩零影响：试玩前后单场记忆用量、检索去重窗口、决策日志、队列**逐项不变**；
- V5.1 试玩返回完整决策（`reason` / `recall` / `degrade` / `latency_ms` 齐全），
  页面不用二次拼装；
- 事件构造器：五类事件的必填项校验、礼物档位与直播用同一套阈值；
- 批量投递上限、整批校验、单条失败不拖垮整批；
- V5.2 聚合卡数值与日志实际条数一致（`test` 记录单独计数，不混进"本场决策"）。

隔离测试刻意用**真 `AgentBrain` + 桩 LLM**（而不是假 brain）：只有真对象才能证明
"记忆与日志没被动过"，假 brain 只能证明"没调用假 brain"。
"""
import asyncio
import json

import pytest

from hub.agent.brain import AgentBrain
from hub.agent.log import DecisionLogger, summarize
from hub.agent.playground import (
    EVENT_TYPES,
    MAX_BATCH,
    Playground,
    PlaygroundError,
    build_batch,
    build_context,
)
from hub.router import GIFT_TIER_HIGH, GIFT_TIER_MID, gift_tier

PROFILE = {
    "samples_ai": [
        {"id": "s1", "enabled": True, "type_name": "问下载",
         "examples": ["怎么下载", "在哪下载啊"],
         "reply_text": "右下角小手柄点一下就能下载"},
    ],
    "topics_ai": [{"id": "t1", "enabled": True, "text": "这游戏主打休闲挂机"}],
}

GOOD = ('{"should_reply": true, "priority": 1, "interrupt": false, "tone": "热情", '
        '"emotion": "happy", "speed": 1.0, "reason": "新观众问下载", '
        '"reply_text": "兄弟点右下角小手柄就能下", "memory_note": "答过下载"}')
NO_REPLY = '{"should_reply": false, "reason": "无意义刷屏", "reply_text": ""}'


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


class StubLLM:
    def __init__(self, answers, model="stub-model"):
        self._answers = list(answers)
        self.model = model
        self.calls = []

    async def ask(self, system, user):
        self.calls.append({"system": system, "user": user})
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class StubSelector:
    def __init__(self, emotions=("default", "happy")):
        self.profile = PROFILE
        self._emotions = list(emotions)

    def build_system_prompt(self, base_cfg=None):
        return "你是抖音游戏主播「仔仔鼠」"

    def current_emotions(self):
        return list(self._emotions)

    def max_reply_chars(self, default=40):
        return 30

    def resolve_audio(self, item, emotion=""):
        return ""


def make_brain(tmp_path, answers, stats=None):
    return AgentBrain(
        config={"llm": {"base_url": "u", "api_key": "k", "model": "m"}},
        selector=StubSelector(),
        llm=StubLLM(answers),
        decision_log=DecisionLogger(log_dir=tmp_path, enabled=True),
        stats=stats)


def live_chat(content="这游戏怎么下载", user="阿强"):
    return {"type": "chat", "user": user, "content": content,
            "merged_count": 1, "members": [(user, content)],
            "event_ts": 3600.0, "test": False}


# ---------------------------------------------------------------- 事件构造器


def test_build_context_chat_shape():
    ctx = build_context({"type": "chat", "user": " 阿强 ", "content": "在吗"})
    assert ctx["type"] == "chat"
    assert ctx["user"] == "阿强"                       # 去空白
    assert ctx["content"] == "在吗"
    assert ctx["members"] == [("阿强", "在吗")]        # describe_event 靠它渲染弹幕行
    assert ctx["test"] is True                        # 试玩一律标记测试


def test_build_context_gift_carries_tier_same_as_live():
    """礼物档位与直播同一套阈值（`router.gift_tier`），否则页面与直播口径会打架。"""
    low = build_context({"type": "gift", "user": "小明", "gift_name": "小心意",
                         "gift_count": 3, "gift_value": GIFT_TIER_MID - 1})
    mid = build_context({"type": "gift", "user": "小明", "gift_name": "玫瑰",
                         "gift_value": 520})
    high = build_context({"type": "gift", "user": "小明", "gift_name": "嘉年华",
                          "gift_value": 2000})
    assert (low["tier"], mid["tier"], high["tier"]) == ("low", "mid", "high")
    assert mid["tier"] == gift_tier(520)
    assert high["tier"] == gift_tier(2000)
    assert low["gift_count"] == 3


def test_build_context_follow_and_enter_need_no_content():
    for kind in ("follow", "enter"):
        ctx = build_context({"type": kind, "user": "小红"})
        assert ctx["type"] == kind and ctx["content"] == ""


@pytest.mark.parametrize("bad, hint", [
    ({}, "事件类型"),
    ({"type": "danmaku", "user": "甲"}, "事件类型"),
    ({"type": "chat", "user": ""}, "昵称"),
    ({"type": "chat", "user": "甲"}, "弹幕内容"),
    ({"type": "gift", "user": "甲", "gift_name": ""}, "礼物名"),
    ({"type": "gift", "user": "甲", "gift_name": "玫瑰", "gift_value": "很贵"}, "数字"),
    ({"type": "gift", "user": "甲", "gift_name": "玫瑰", "gift_value": -5}, "之间"),
    ({"type": "gift", "user": "甲", "gift_name": "玫瑰", "gift_count": 0}, "之间"),
])
def test_build_context_rejects_bad_input(bad, hint):
    with pytest.raises(PlaygroundError) as e:
        build_context(bad)
    assert hint in str(e.value)


def test_build_context_clips_long_fields():
    ctx = build_context({"type": "chat", "user": "很" * 100, "content": "长" * 500})
    assert len(ctx["user"]) == 40 and len(ctx["content"]) == 200


def test_build_batch_numbers_repeated_content():
    batch = build_batch("chat", 3, user="甲", content="666")
    assert [b["content"] for b in batch] == ["666（第 1 条）", "666（第 2 条）", "666（第 3 条）"]
    # build_batch 出的是"表单原始载荷"（与单条表单同形状），校验统一由 build_context 做
    assert [c["user"] for c in map(build_context, batch)] == ["甲"] * 3

    gifts = build_batch("gift", 3, user="甲", gift_name="玫瑰", gift_value=520)
    assert [g["gift_count"] for g in gifts] == [1, 2, 3]
    assert [build_context(g)["tier"] for g in gifts] == ["mid"] * 3


def test_build_batch_single_item_keeps_explicit_count():
    """批量 1 条时不该把用户填的礼物数量改成 1（那是"连击模拟"才需要的改写）。"""
    one = build_batch("gift", 1, user="甲", gift_name="玫瑰", gift_count=5, gift_value=520)
    assert [build_context(g)["gift_count"] for g in one] == [5]


def test_event_types_match_router_value_domain():
    from hub.events import METHOD_TO_TYPE
    assert set(EVENT_TYPES) == set(METHOD_TO_TYPE.values())


# ---------------------------------------------------------------- V5.3 隔离


def test_playground_touches_no_live_state(tmp_path):
    """V5.3：试玩前后记忆用量、去重窗口、决策日志逐项不变。"""
    brain = make_brain(tmp_path, [GOOD, GOOD])
    asyncio.run(brain.handle(live_chat()))          # 先有一条真实决策做基线
    log_path = brain.decision_log.path()
    before_mem = brain.memory.stats()
    before_lines = log_path.read_text(encoding="utf-8").splitlines()
    before_spoken = brain.memory.spoken()
    assert before_mem == {"events": 1, "spoken": 1, "notes": 1}

    pg = Playground(brain, clock=lambda: 1000.0)
    out = asyncio.run(pg.run([{"type": "chat", "user": "甲", "content": "这游戏怎么下载"}]))

    assert out["results"][0]["should_reply"] is True
    assert brain.memory.stats() == before_mem           # 记忆没被写
    assert brain.memory.spoken() == before_spoken
    assert log_path.read_text(encoding="utf-8").splitlines() == before_lines  # 日志没被写


def test_playground_does_not_pollute_recall_dedup_window(tmp_path):
    """试玩不写"刚说过"窗口——否则试玩一句会把真实直播的素材召回给堵掉。"""
    brain = make_brain(tmp_path, [GOOD, GOOD])
    pg = Playground(brain, clock=lambda: 1000.0)
    asyncio.run(pg.run([{"type": "chat", "user": "甲", "content": "怎么下载"}]))

    brain.retriever.index(PROFILE, None)
    ids = [r["id"] for r in brain.retriever.retrieve("这游戏怎么下载")]
    assert "sample:s1" in ids                          # 素材仍在可选池里


def test_playground_reads_live_memory(tmp_path):
    """隔离 = 不写，不是不读：试玩要看的是"此刻这条事件它会怎么回"。"""
    brain = make_brain(tmp_path, [GOOD, GOOD])
    asyncio.run(brain.handle(live_chat("主播什么时候开播")))
    pg = Playground(brain, clock=lambda: 1000.0)
    asyncio.run(pg.run([{"type": "chat", "user": "乙", "content": "在吗"}]))

    system = brain._llm.calls[-1]["system"]
    assert "【本场直播记忆】" in system
    assert "主播什么时候开播" in system                 # 真实直播那条在上下文里


# ---------------------------------------------------------------- V5.1 输出


def test_preview_returns_full_decision_record(tmp_path):
    """V5.1：一条就能看到"为什么这么回"+ 素材引用，页面不用二次拼装。"""
    brain = make_brain(tmp_path, [GOOD])
    pg = Playground(brain, clock=lambda: 1000.0)
    out = asyncio.run(pg.run([{"type": "chat", "user": "阿强", "content": "这游戏怎么下载"}]))
    item = out["results"][0]

    assert item["index"] == 0 and item["error"] == ""
    assert item["event_line"] == "阿强：这游戏怎么下载"
    assert item["should_reply"] is True
    assert item["reason"] == "新观众问下载"
    assert item["tone"] == "热情"
    assert item["reply_text"] == "兄弟点右下角小手柄就能下"
    assert item["emotion"] == "happy" and item["priority"] == 1
    assert "sample:s1" in item["recall"]
    assert item["model"] == "stub-model" and item["latency_ms"] >= 0
    assert item["test"] is True and item["prerecorded"] is False


def test_preview_should_reply_false_is_reported_not_swallowed(tmp_path):
    brain = make_brain(tmp_path, [NO_REPLY])
    pg = Playground(brain, clock=lambda: 1000.0)
    out = asyncio.run(pg.run([{"type": "chat", "user": "甲", "content": "666"}]))
    item = out["results"][0]
    assert item["should_reply"] is False
    assert item["reply_text"] == ""
    assert item["reason"] == "无意义刷屏"
    assert out["summary"]["no_reply"] == 1


# ---------------------------------------------------------------- 批量与容错


def test_batch_covers_every_event_type(tmp_path):
    brain = make_brain(tmp_path, [GOOD, GOOD, GOOD, GOOD, GOOD])
    pg = Playground(brain, clock=lambda: 1000.0)
    out = asyncio.run(pg.run([
        {"type": "chat", "user": "甲", "content": "怎么下载"},
        {"type": "gift", "user": "乙", "gift_name": "嘉年华", "gift_value": 2000},
        {"type": "follow", "user": "丙"},
        {"type": "fansclub", "user": "丁", "content": "冲"},
        {"type": "enter", "user": "戊"},
    ]))
    assert [r["type"] for r in out["results"]] == list(EVENT_TYPES)
    assert out["summary"]["count"] == 5
    assert out["summary"]["by_type"]["gift"] == 1
    assert out["results"][1]["tier"] == "high"


def test_over_batch_limit_is_rejected(tmp_path):
    brain = make_brain(tmp_path, [GOOD])
    pg = Playground(brain, clock=lambda: 1000.0)
    items = [{"type": "chat", "user": "甲", "content": f"第{i}条"} for i in range(MAX_BATCH + 1)]
    with pytest.raises(PlaygroundError) as e:
        asyncio.run(pg.run(items))
    assert str(MAX_BATCH) in str(e.value)


def test_empty_batch_is_rejected(tmp_path):
    brain = make_brain(tmp_path, [GOOD])
    pg = Playground(brain, clock=lambda: 1000.0)
    with pytest.raises(PlaygroundError):
        asyncio.run(pg.run([]))


def test_invalid_item_rejects_whole_batch(tmp_path):
    """整批校验：不产生"跑了前三条才发现第四条错"的半截结果。"""
    brain = make_brain(tmp_path, [GOOD, GOOD])
    pg = Playground(brain, clock=lambda: 1000.0)
    with pytest.raises(PlaygroundError):
        asyncio.run(pg.run([{"type": "chat", "user": "甲", "content": "在吗"},
                            {"type": "gift", "user": "乙"}]))
    assert brain._llm.calls == []                       # 一条都没跑


def test_single_failure_does_not_abort_batch(tmp_path):
    from hub.agent.llm import AgentLLMError

    brain = make_brain(tmp_path, [GOOD, AgentLLMError("上游 502")])
    pg = Playground(brain, clock=lambda: 1000.0)
    out = asyncio.run(pg.run([{"type": "chat", "user": "甲", "content": "在吗"},
                              {"type": "chat", "user": "乙", "content": "还在吗"}]))
    assert out["results"][0]["error"] == ""
    assert "AgentLLMError" in out["results"][1]["error"]
    assert out["results"][1]["should_reply"] is False
    assert out["summary"]["failed"] == 1


def test_unavailable_when_brain_missing():
    pg = Playground(None)
    assert pg.available is False
    with pytest.raises(PlaygroundError) as e:
        asyncio.run(pg.run([{"type": "chat", "user": "甲", "content": "在吗"}]))
    assert "未装配" in str(e.value)


# ---------------------------------------------------------------- V5.2 聚合卡


def _record(**over):
    base = {"ts": "2026-09-22T23:41:07.000", "type": "chat", "user": "阿强",
            "should_reply": True, "priority": 1, "interrupt": False,
            "reason": "有效提问", "reply_text": "收到", "emotion": "happy",
            "degrade": "", "latency_ms": 1200, "prerecorded": False, "test": False}
    base.update(over)
    return base


def test_summarize_counts_match_records():
    """V5.2：聚合卡数值与日志实际条数一致。"""
    records = [_record(), _record(type="gift", should_reply=False, latency_ms=800),
               _record(type="gift", prerecorded=True, degrade="salvage", latency_ms=1000)]
    got = summarize(records)
    assert got["total"] == 3 and got["replied"] == 2 and got["no_reply"] == 1
    assert got["by_type"] == {"chat": 1, "gift": 2}
    assert got["avg_latency_ms"] == 1000
    assert got["degraded"] == 1 and got["prerecorded"] == 1


def test_summarize_excludes_test_records_from_live_stats():
    records = [_record(), _record(test=True, latency_ms=99999), _record(test=True)]
    got = summarize(records)
    assert got["total"] == 1                    # 本场决策只算真实直播
    assert got["test_skipped"] == 2
    assert got["avg_latency_ms"] == 1200        # 测试那条 99999ms 不参与平均
    assert got["by_type"] == {"chat": 1}


def test_summarize_on_empty_log():
    got = summarize([])
    assert got["total"] == 0 and got["avg_latency_ms"] == 0
    assert got["no_reply_ratio"] == 0.0 and got["by_type"] == {}


def test_summarize_ratio_rounding():
    records = [_record()] + [_record(should_reply=False) for _ in range(2)]
    assert summarize(records)["no_reply_ratio"] == 66.7


def test_summarize_reads_real_log_file(tmp_path):
    """真日志闭环：落盘 → 读回 → 聚合，条数必须对得上。"""
    logger = DecisionLogger(log_dir=tmp_path, enabled=True)
    logger.write(_record())
    logger.write(_record(type="follow"))
    logger.write(_record(test=True))
    got = summarize(logger.read_recent(500))
    assert got["total"] == 2 and got["test_skipped"] == 1
    assert sum(got["by_type"].values()) == got["total"]


def test_read_recent_limit_still_honoured(tmp_path):
    logger = DecisionLogger(log_dir=tmp_path, enabled=True)
    for _ in range(5):
        logger.write(_record())
    assert len(logger.read_recent(2)) == 2
    assert len(logger.read_recent(500)) == 5
    assert json.loads(logger.path().read_text(encoding="utf-8").splitlines()[0])["type"] == "chat"
