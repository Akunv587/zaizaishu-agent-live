"""反馈采集入口的单元测试（M6-3）。

分两层：

**A. 反馈上下文只在智能体决策上出现**（`Router._emit_agent_result`）
判定条件是"结果里有没有 `reason`"，它正好把三条链路分开，于是"开关 OFF 时行为与
旧版逐字一致"不需要额外开关就成立：

| 链路 | 结果形态 | 挂按钮 |
|---|---|---|
| 智能体 | dict 九字段（有 reason） | 挂 |
| v3 模板方案 | dict（text / audio_path / priority） | 不挂 |
| 规则 / 兜底 | str 或 {"text","priority"} | 不挂 |

**B. 三个按钮落到文件**（`WebUi._ag_feedback`）
采纳 / 弃用 / 改写各自写进 `examples.jsonl` + `feedback.jsonl`，失败回 `ok:false`
的帧与提示，不抛到调用方。
"""
import asyncio

import pytest

from hub import logger as logger_mod
from hub.agent.feedback import EXAMPLES_FILE, FEEDBACK_FILE, FeedbackStore
from hub.events import LiveEvent
from hub.router import MODE_AGENT, MODE_RULES, Router
from hub.webui import WebUi


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SeqRandom:
    def __init__(self, values=(0.5,)):
        self.values = list(values)
        self.i = 0

    def random(self):
        v = self.values[min(self.i, len(self.values) - 1)]
        self.i += 1
        return v


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


def make_router(tasks, clock, mode=MODE_AGENT, handler=None, **cfg_over):
    cfg = {
        "events": {
            "chat": {"enabled": True, "merge_window_s": 3, "ignore_noise_p": 1.0},
            "gift": {"enabled": True, "combo_timeout_s": 5},
            "follow": {"enabled": True, "cooldown_s": 30, "probability": 0.6},
            "fansclub": {"enabled": True, "cooldown_s": 30, "probability": 0.8},
            "enter": {"enabled": True},
        },
        "safety": {"fallback_reply": "哈哈这个咱们直播间聊哈"},
        "agent": {"feedback": {"enabled": True}},
    }
    cfg.update(cfg_over)
    router = Router(tasks.append, config=cfg, clock=clock, random=SeqRandom(),
                    reply_builder=lambda ctx: f"回复：{ctx.get('content', '')}",
                    input_filter=None, mode=mode)
    if handler is not None:
        router.set_agent_handler(handler)
    return router


def agent_result(text="地铁跑酷，来一起玩", reason="有效提问"):
    """智能体决策的返回形态（`AgentBrain._to_result` 的真形状）。"""
    return {"text": text, "audio_path": "", "emotion": "happy", "speed": None,
            "priority": 1, "interrupt": False, "tone": "热情", "reason": reason}


# ================================================================ A. 上下文


def test_agent_decision_carries_feedback_context(logger_setup):
    tasks = []
    clock = FakeClock()
    router = make_router(tasks, clock, handler=lambda ctx: agent_result())
    router.handle_event(LiveEvent(type="chat", user="小明", content="主播玩什么游戏",
                            ts=clock()))

    assert len(tasks) == 1
    fb = tasks[0].feedback
    assert fb is not None
    assert fb["event_type"] == "chat"
    assert fb["user"] == "小明"
    assert fb["content"] == "主播玩什么游戏"
    assert fb["reply"] == "地铁跑酷，来一起玩"
    assert fb["tone"] == "热情"
    assert fb["reason"] == "有效提问"


def test_rules_text_result_has_no_feedback(logger_setup):
    """rules 链路返回纯字符串 → 不挂按钮（开关 OFF 行为逐字不变）。"""
    tasks = []
    clock = FakeClock()
    router = make_router(tasks, clock, mode=MODE_RULES)
    router.handle_event(LiveEvent(type="chat", user="小明", content="在吗", ts=clock()))
    clock.advance(5)
    router.scan(clock())                 # rules 模式走聚合窗口：等窗口关闭才产出
    assert tasks and tasks[0].feedback is None
    assert tasks[0].text == "回复：在吗"


def test_template_dict_without_reason_has_no_feedback(logger_setup):
    """模板方案的 dict（有 text / audio_path，无 reason）同样不挂。"""
    tasks = []
    clock = FakeClock()
    router = make_router(tasks, clock, handler=lambda ctx: {
        "text": "谢谢关注", "audio_path": "", "priority": 1})
    router.handle_event(LiveEvent(type="follow", user="小红", ts=clock()))
    assert tasks and tasks[0].feedback is None


def test_fallback_dict_without_reason_has_no_feedback(logger_setup):
    """兜底口播是 {"text", "priority"}，不能长出反馈按钮。"""
    tasks = []
    clock = FakeClock()

    def boom(ctx):
        raise RuntimeError("模型挂了")

    router = make_router(tasks, clock, handler=boom)
    router.handle_event(LiveEvent(type="chat", user="小明", content="在吗", ts=clock()))
    assert tasks and tasks[0].text == "哈哈这个咱们直播间聊哈"
    assert tasks[0].feedback is None


def test_feedback_disabled_in_config_removes_context(logger_setup):
    """`agent.feedback.enabled: false` → 不挂按钮（厂长级开关）。"""
    tasks = []
    clock = FakeClock()
    router = make_router(tasks, clock, handler=lambda ctx: agent_result(),
                         agent={"feedback": {"enabled": False}})
    router.handle_event(LiveEvent(type="chat", user="小明", content="在吗", ts=clock()))
    assert tasks and tasks[0].feedback is None


def test_gift_feedback_context_carries_gift_fields(logger_setup):
    """礼物事件的 content 为空，靠 gift_name / gift_count 落成「火箭 ×3」。"""
    tasks = []
    clock = FakeClock()
    router = make_router(tasks, clock, handler=lambda ctx: agent_result(
        text="谢谢大哥的火箭！", reason="大额礼物"))
    router.handle_event(LiveEvent(type="gift", user="大哥", gift_name="火箭",
                            gift_count=3, gift_value=1000, ts=clock()))
    fb = tasks[0].feedback
    assert fb["event_type"] == "gift"
    assert fb["gift_name"] == "火箭" and fb["gift_count"] == 3
    assert fb["content"] == ""


def test_feedback_context_roundtrips_into_store(tmp_path):
    """真产物直通：router 组装的 feedback 原样交给 FeedbackStore，字段不丢。"""
    tasks = []
    clock = FakeClock()
    router = make_router(tasks, clock, handler=lambda ctx: agent_result())
    router.handle_event(LiveEvent(type="chat", user="小明", content="主播玩什么游戏",
                            ts=clock()))

    store = FeedbackStore(root=tmp_path, pid_provider=lambda: "p_t1")
    rec = store.adopt(tasks[0].feedback)
    assert rec["event"] == {"content": "主播玩什么游戏"}
    assert rec["reply"] == "地铁跑酷，来一起玩"
    assert rec["tone"] == "热情" and rec["reason"] == "有效提问"


def test_webui_feed_entry_carries_feedback(tmp_path, logger_setup):
    """消息流条目带上反馈上下文，前端才有东西可渲染。"""
    from hub.queue import PrioritySpeakQueue
    from hub.stats import StatsCollector

    clock = FakeClock()
    tasks = []
    router = make_router(tasks, clock, handler=lambda ctx: agent_result())
    ui = WebUi(config={}, stats=StatsCollector(),
               queue=PrioritySpeakQueue(), clock=clock)
    ui.player_notes_hook = None
    router.handle_event(LiveEvent(type="chat", user="小明", content="主播玩什么游戏",
                            ts=clock()))
    entry_task = tasks[0]
    ui.note_play_start(entry_task)

    entry = ui._feed[-1]
    assert entry["kind"] == "play_start"
    assert entry["feedback"]["reply"] == "地铁跑酷，来一起玩"
    assert entry["feedback"]["reason"] == "有效提问"


def test_feed_entry_without_feedback_stays_clean(tmp_path, logger_setup):
    """rules 任务 → 条目里没有 feedback 键（前端据此不渲染按钮）。"""
    from hub.events import SpeakTask
    from hub.queue import PrioritySpeakQueue
    from hub.stats import StatsCollector

    ui = WebUi(config={}, stats=StatsCollector(), queue=PrioritySpeakQueue(),
               clock=FakeClock())
    ui.note_play_start(SpeakTask(priority=1, text="在的宝子", event_ts=0.0,
                                 source="chat"))
    assert "feedback" not in ui._feed[-1]


# ================================================================ B. 动作族


class CollectingWebUi(WebUi):
    """把广播帧收进列表（`_broadcast` 是唯一出口）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.frames = []

    def _broadcast(self, msg: dict) -> None:
        self.frames.append(msg)

    def last(self, kind: str) -> dict:
        hits = [f for f in self.frames if f.get("type") == kind]
        assert hits, f"没有 {kind} 帧：{[f.get('type') for f in self.frames]}"
        return hits[-1]


def make_ui(tmp_path, pid="p_t1"):
    from hub.queue import PrioritySpeakQueue
    from hub.stats import StatsCollector

    ui = CollectingWebUi(config={}, stats=StatsCollector(),
                         queue=PrioritySpeakQueue(), clock=FakeClock())
    ui.feedback_store = FeedbackStore(root=tmp_path, pid_provider=lambda: pid)
    return ui


ENTRY = {"event_type": "chat", "user": "小明", "content": "主播玩什么游戏",
         "reply": "地铁跑酷，来一起玩", "tone": "热情", "reason": "有效提问"}


def test_action_adopt_writes_and_replies(tmp_path, logger_setup):
    ui = make_ui(tmp_path)
    asyncio.run(ui._ag_feedback({"kind": "adopt", "feedback": ENTRY}))

    frame = ui.last("agent_feedback_done")
    assert frame["ok"] is True
    assert frame["data"]["feedback_id"].startswith("fb_")
    assert frame["data"]["stats"]["adopted"] == 1
    path = tmp_path / "p_t1" / "agent" / EXAMPLES_FILE
    assert path.is_file() and ENTRY["reply"] in path.read_text(encoding="utf-8")


def test_action_reject_records_reasons(tmp_path, logger_setup):
    ui = make_ui(tmp_path)
    asyncio.run(ui._ag_feedback({"kind": "reject", "feedback": ENTRY,
                                 "reasons": ["太长"], "note": "啰嗦"}))
    frame = ui.last("agent_feedback_done")
    assert frame["ok"] is True and "太长" in frame["data"]["message"]
    text = (tmp_path / "p_t1" / "agent" / FEEDBACK_FILE).read_text(encoding="utf-8")
    assert '"reasons": ["太长"]' in text.replace("'", '"') or "太长" in text
    assert frame["data"]["stats"]["rejected"] == 1


def test_action_reject_without_reason_fails_softly(tmp_path, logger_setup):
    """弃用不选原因也不写备注 → ok:false + 提示，不抛异常。"""
    ui = make_ui(tmp_path)
    asyncio.run(ui._ag_feedback({"kind": "reject", "feedback": ENTRY, "reasons": []}))
    frame = ui.last("agent_feedback_done")
    assert frame["ok"] is False and "原因" in frame["data"]["message"]
    assert not (tmp_path / "p_t1" / "agent" / EXAMPLES_FILE).exists()


def test_action_rewrite_keeps_original(tmp_path, logger_setup):
    ui = make_ui(tmp_path)
    asyncio.run(ui._ag_feedback({"kind": "rewrite", "feedback": ENTRY,
                                 "text": "地铁跑酷，来猜猜我几分"}))
    frame = ui.last("agent_feedback_done")
    assert frame["ok"] is True
    store = ui.feedback_store
    rec = store.all_examples()[-1]
    assert rec["source"] == "rewritten"
    assert rec["reply"] == "地铁跑酷，来猜猜我几分"
    assert rec["original_reply"] == ENTRY["reply"]


def test_action_rewrite_empty_text_fails_softly(tmp_path, logger_setup):
    ui = make_ui(tmp_path)
    asyncio.run(ui._ag_feedback({"kind": "rewrite", "feedback": ENTRY, "text": " "}))
    assert ui.last("agent_feedback_done")["ok"] is False


def test_action_unknown_kind_or_missing_context(tmp_path, logger_setup):
    ui = make_ui(tmp_path)
    asyncio.run(ui._ag_feedback({"kind": "like", "feedback": ENTRY}))
    assert ui.last("agent_feedback_done")["ok"] is False
    asyncio.run(ui._ag_feedback({"kind": "adopt"}))
    assert ui.last("agent_feedback_done")["ok"] is False


def test_action_without_store_fails_softly(tmp_path, logger_setup):
    from hub.queue import PrioritySpeakQueue
    from hub.stats import StatsCollector

    ui = CollectingWebUi(config={}, stats=StatsCollector(),
                         queue=PrioritySpeakQueue(), clock=FakeClock())
    asyncio.run(ui._ag_feedback({"kind": "adopt", "feedback": ENTRY}))
    frame = ui.last("agent_feedback_done")
    assert frame["ok"] is False and "未装配" in frame["data"]["message"]


def test_action_broadcasts_via_dispatch_table(tmp_path, logger_setup):
    """动作表接线：`ag_feedback` / `ag_feedback_stats` 都在分发里，不是未知动作。"""
    import json

    ui = make_ui(tmp_path)
    seen = []
    ui._broadcast = lambda msg: seen.append(msg)

    async def scenario():
        # `ag_feedback` 走 create_task：同一次循环里要让出去，任务才会真正跑
        await ui._handle_action(json.dumps(
            {"action": "ag_feedback", "kind": "adopt", "feedback": ENTRY}))
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert any(f.get("type") == "agent_feedback_done" for f in seen), \
        [f.get("type") for f in seen]

    seen.clear()
    asyncio.run(ui._handle_action(json.dumps({"action": "ag_feedback_stats"})))
    assert any(f.get("type") == "agent_feedback_stats" for f in seen), \
        [f.get("type") for f in seen]


def test_stats_action_shapes(tmp_path, logger_setup):
    ui = make_ui(tmp_path)
    asyncio.run(ui._ag_feedback({"kind": "adopt", "feedback": ENTRY}))
    asyncio.run(ui._ag_feedback({"kind": "reject", "feedback": ENTRY,
                                 "reasons": ["跑题"]}))
    ui._ag_push_feedback_stats()
    data = ui.last("agent_feedback_stats")["data"]
    assert data["available"] is True
    s = data["stats"]
    assert s["total"] == 2 and s["adopted"] == 1 and s["rejected"] == 1
    assert s["adopt_rate"] == 50.0 and s["reasons"] == {"跑题": 1}


def test_stats_action_without_store(tmp_path, logger_setup):
    from hub.queue import PrioritySpeakQueue
    from hub.stats import StatsCollector

    ui = CollectingWebUi(config={}, stats=StatsCollector(),
                         queue=PrioritySpeakQueue(), clock=FakeClock())
    ui._ag_push_feedback_stats()
    frame = ui.last("agent_feedback_stats")
    assert frame["ok"] is False and "未装配" in frame["data"]["note"]
