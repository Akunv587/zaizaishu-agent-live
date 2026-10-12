"""接管钩子推广到全事件类型的单测（M4-4，DESIGN §5.1）。

背景：M3 只把 `set_agent_handler` 挂在弹幕出口（`_handle_chat` → `_dispatch_context`），
agent 模式下礼物/关注/粉丝团/进场仍走 `templates.render` 的本地模板，绕过智能体——
与 DESIGN §5.1「每条都产出，**怎么感谢交智能体**」不符。M4 把钩子推广到全部五类。

验收口径：
- 礼物/关注/粉丝团/进场在 agent 模式下都进智能体；
- `_agent_active()` 两个条件（mode + 钩子）缺一不可：只切 mode 回落模板，只挂钩子
  但仍 rules 时规则链路原样生效；
- 逐类事件开关（`event_enabled`，用户显式配置）**在智能体之前**仍然生效；
- 决策给的优先级/插队落地：`interrupt` 仅在 `priority=0` 时有效；
- 钩子异常 → 兜底口播（末级容错），不丢事件、不静默。
"""
import asyncio

import pytest

from hub.events import LiveEvent
from hub.router import (
    GIFT_TIER_HIGH,
    GIFT_TIER_MID,
    MODE_AGENT,
    MODE_RULES,
    Router,
)


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SeqRandom:
    def __init__(self, values):
        self.values = list(values)
        self.i = 0

    def random(self):
        if self.i >= len(self.values):
            return self.values[-1]
        v = self.values[self.i]
        self.i += 1
        return v


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


def make_router(tasks, clock, mode=MODE_AGENT, handler=None, template_renderer=None,
                **cfg_over):
    cfg = {
        "events": {
            "chat": {"enabled": True, "merge_window_s": 3, "ignore_noise_p": 1.0},
            "gift": {"enabled": True, "combo_timeout_s": 5},
            "follow": {"enabled": True, "cooldown_s": 30, "probability": 0.6},
            "fansclub": {"enabled": True, "cooldown_s": 30, "probability": 0.8},
            "enter": {"enabled": True},
        },
        "safety": {"fallback_reply": "哈哈这个咱们直播间聊哈"},
    }
    cfg.update(cfg_over)
    router = Router(tasks.append, config=cfg, clock=clock,
                    random=SeqRandom([0.5]),
                    reply_builder=lambda ctx: f"回复：{ctx.get('content', '')}",
                    template_renderer=template_renderer,
                    input_filter=None, mode=mode)
    if handler is not None:
        router.set_agent_handler(handler)
    return router


def gift(user, count, ts, name="小心意", price=50, combo_end=False):
    return LiveEvent(type="gift", user=user, gift_name=name, gift_count=count,
                     gift_value=price, ts=ts, combo_end=combo_end)


# ---------------------------------------------------------------- 礼物


def test_gift_in_agent_mode_reaches_handler_with_full_context():
    seen = []
    tasks, clock = [], FakeClock(0.0)
    router = make_router(
        tasks, clock,
        handler=lambda ctx: (seen.append(ctx),
                             {"text": "谢谢兄弟的火箭", "priority": 0, "interrupt": True})[1])
    router.handle_event(gift("小明", 2, 10.0, name="火箭", price=GIFT_TIER_HIGH))

    assert len(seen) == 1
    ctx = seen[0]
    assert ctx["type"] == "gift"
    assert (ctx["gift_name"], ctx["gift_count"], ctx["gift_value"]) == ("火箭", 2, GIFT_TIER_HIGH)
    assert ctx["tier"] == "high"
    assert ctx["event_ts"] == 10.0            # 取源事件时间戳
    assert len(tasks) == 1
    assert (tasks[0].source, tasks[0].priority, tasks[0].interrupt) == ("gift", 0, True)


def test_gift_decision_defaults_to_p0_without_explicit_priority():
    """决策没给 priority 时按该类型的规则档位兜底：礼物 P0。"""
    tasks, clock = [], FakeClock()
    router = make_router(tasks, clock, handler=lambda ctx: {"text": "谢谢"})
    router.handle_event(gift("小明", 1, 1.0))
    assert tasks[0].source == "gift"
    assert tasks[0].priority == 0
    assert tasks[0].interrupt is False        # 没让插队就不插队


def test_gift_should_reply_false_emits_nothing():
    tasks, clock = [], FakeClock()
    router = make_router(tasks, clock, handler=lambda ctx: None)
    router.handle_event(gift("小明", 1, 1.0))
    assert tasks == []


def test_gift_tier_switch_still_gates_before_agent():
    """逐类事件开关是用户显式配置（DESIGN §六 第 17 条），在智能体之前生效。"""
    seen = []
    tasks, clock = [], FakeClock()
    router = make_router(tasks, clock, handler=lambda ctx: seen.append(ctx) or None)
    router.set_event_enabled({"gift_high": False})
    router.handle_event(gift("小明", 1, 1.0, price=GIFT_TIER_HIGH))
    assert seen == [] and tasks == []
    router.handle_event(gift("小红", 1, 2.0, price=GIFT_TIER_MID))   # 中额未被关
    assert len(seen) == 1


def test_gift_tier_switch_bypassed_for_test_events():
    seen = []
    tasks, clock = [], FakeClock()
    router = make_router(tasks, clock, handler=lambda ctx: seen.append(ctx) or None)
    router.set_event_enabled({"gift_high": False})
    ev = gift("小明", 1, 1.0, price=GIFT_TIER_HIGH)
    ev.test = True
    router.handle_event(ev)
    assert len(seen) == 1


# ---------------------------------------------------------------- 关注 / 粉丝团 / 进场


def test_follow_and_fansclub_reach_handler_without_cooldown():
    """agent 模式下关注/粉丝团逐条进智能体（冷却与概率放开，DESIGN §5.1）。"""
    seen = []
    tasks, clock = [], FakeClock()
    router = make_router(tasks, clock,
                         handler=lambda ctx: seen.append(ctx["type"]) or {"text": "欢迎"})
    for i in range(3):
        router.handle_event(LiveEvent(type="follow", user=f"u{i}", ts=float(i)))
    router.handle_event(LiveEvent(type="fansclub", user="小李", content="冲", ts=9.0))
    assert seen == ["follow", "follow", "follow", "fansclub"]
    assert len(tasks) == 4
    assert [t.source for t in tasks] == ["follow", "follow", "follow", "fansclub"]


def test_follow_context_carries_user_and_content():
    seen = []
    tasks, clock = [], FakeClock()
    router = make_router(tasks, clock, handler=lambda ctx: seen.append(ctx) or None)
    router.handle_event(LiveEvent(type="fansclub", user="小李", content="冲鸭", ts=3.0))
    assert seen[0]["user"] == "小李"
    assert seen[0]["content"] == "冲鸭"
    assert seen[0]["event_ts"] == 3.0


def test_enter_reaches_handler_when_enabled():
    seen = []
    tasks, clock = [], FakeClock()
    router = make_router(tasks, clock,
                         handler=lambda ctx: seen.append(ctx["type"]) or {"text": "欢迎"})
    router.handle_event(LiveEvent(type="enter", user="小张", ts=1.0))
    assert seen == ["enter"]
    assert tasks[0].source == "enter"


def test_enter_still_gated_by_event_switch():
    seen = []
    tasks, clock = [], FakeClock()
    router = make_router(tasks, clock, handler=lambda ctx: seen.append(ctx) or None,
                         events={"enter": {"enabled": False}})
    router.handle_event(LiveEvent(type="enter", user="小张", ts=1.0))
    assert seen == [] and tasks == []


# ---------------------------------------------------------------- 双条件守卫


def test_handler_attached_but_rules_mode_keeps_rule_chain():
    """只挂钩子、模式仍 rules：聚合/连击/模板必须原样生效，事件不给智能体。"""
    seen = []
    tasks, clock = [], FakeClock()
    router = make_router(tasks, clock, mode=MODE_RULES,
                         handler=lambda ctx: seen.append(ctx) or {"text": "不该出现"},
                         template_renderer=lambda kind, fields: f"[{kind}]{fields['user']}")
    router.handle_event(gift("小明", 1, 0.0, price=50, combo_end=True))
    assert seen == []
    assert len(tasks) == 1
    assert tasks[0].text == "[gift]小明"       # 走的是本地模板
    assert router.agent_attached is True


def test_mode_agent_without_handler_falls_back_to_template():
    """M3 的降级路径不变：agent 模式但钩子缺失时，礼物仍逐条产出（本地模板）。"""
    tasks, clock = [], FakeClock()
    router = make_router(tasks, clock, mode=MODE_AGENT, handler=None,
                         template_renderer=lambda kind, fields: f"[{kind}]{fields['user']}")
    router.handle_event(gift("小明", 1, 1.0, price=50))
    assert len(tasks) == 1 and tasks[0].text == "[gift]小明"


def test_agent_active_requires_both_conditions():
    tasks, clock = [], FakeClock()
    router = make_router(tasks, clock, mode=MODE_AGENT, handler=lambda ctx: None)
    assert router._agent_active() is True
    router.set_mode(MODE_RULES)
    assert router._agent_active() is False
    router.set_mode(MODE_AGENT)
    router.set_agent_handler(None)
    assert router._agent_active() is False
    assert router.agent_attached is False


# ---------------------------------------------------------------- 优先级与插队


def test_priority_and_interrupt_land_on_task():
    tasks, clock = [], FakeClock()
    router = make_router(
        tasks, clock,
        handler=lambda ctx: {"text": "插队说一句", "priority": 0, "interrupt": True})
    router.handle_event(LiveEvent(type="chat", user="甲", content="在吗", ts=1.0))
    assert (tasks[0].priority, tasks[0].interrupt) == (0, True)


def test_interrupt_ignored_when_priority_is_not_zero():
    """DESIGN §5.3：插队只对 priority=0 生效。"""
    tasks, clock = [], FakeClock()
    router = make_router(
        tasks, clock,
        handler=lambda ctx: {"text": "慢慢说", "priority": 2, "interrupt": True})
    router.handle_event(LiveEvent(type="chat", user="甲", content="在吗", ts=1.0))
    assert tasks[0].priority == 2
    assert tasks[0].interrupt is False


def test_out_of_range_priority_is_clamped():
    tasks, clock = [], FakeClock()
    router = make_router(tasks, clock, handler=lambda ctx: {"text": "x", "priority": 9})
    router.handle_event(LiveEvent(type="chat", user="甲", content="在吗", ts=1.0))
    assert tasks[0].priority == 2


def test_chat_decision_carries_emotion_and_speed():
    tasks, clock = [], FakeClock()
    router = make_router(
        tasks, clock,
        handler=lambda ctx: {"text": "嘿嘿", "emotion": "happy", "speed": 1.1,
                             "audio_path": "C:/a.wav"})
    router.handle_event(LiveEvent(type="chat", user="甲", content="在吗", ts=1.0))
    task = tasks[0]
    assert (task.emotion, task.speed, task.audio_path) == ("happy", 1.1, "C:/a.wav")


# ---------------------------------------------------------------- 异常容错


def test_handler_exception_emits_fallback_reply():
    def boom(context):
        raise RuntimeError("智能体炸了")

    tasks, clock = [], FakeClock()
    router = make_router(tasks, clock, handler=boom)
    router.handle_event(gift("小明", 1, 1.0))
    assert len(tasks) == 1
    assert tasks[0].text == "哈哈这个咱们直播间聊哈"
    assert tasks[0].source == "gift"
    assert tasks[0].priority == 1              # 兜底口播不插队


def test_handler_exception_with_fallback_disabled_is_silent():
    def boom(context):
        raise RuntimeError("智能体炸了")

    tasks, clock = [], FakeClock()
    router = make_router(tasks, clock, handler=boom,
                         safety={"fallback_reply": "x", "fallback_enabled": False})
    router.handle_event(gift("小明", 1, 1.0))
    assert tasks == []


# ---------------------------------------------------------------- 异步决策


def test_async_decision_is_scheduled_and_emitted():
    async def handler(context):
        await asyncio.sleep(0)
        return {"text": "异步回的", "priority": 1}

    tasks, clock = [], FakeClock()

    async def scenario():
        router = make_router(tasks, clock, handler=handler)
        router.handle_event(LiveEvent(type="chat", user="甲", content="在吗", ts=1.0))
        assert tasks == []                     # 还没补齐
        assert router.has_pending_reply() is True
        await router.drain()
        return router

    asyncio.run(scenario())
    assert len(tasks) == 1 and tasks[0].text == "异步回的"


def test_async_gift_decision_does_not_count_as_pending_chat_reply():
    """礼物/关注的在途决策不算"有待回复的弹幕"——话题让位判据只认弹幕（agent 模式下
    P2 让位本就放开）。"""
    async def handler(context):
        await asyncio.sleep(0)
        return {"text": "谢谢"}

    tasks, clock = [], FakeClock()

    async def scenario():
        router = make_router(tasks, clock, handler=handler)
        router.handle_event(gift("小明", 1, 1.0))
        assert router.has_pending_reply() is False
        await router.drain()
        return router

    asyncio.run(scenario())
    assert len(tasks) == 1 and tasks[0].source == "gift"


def test_async_handler_exception_falls_back():
    async def boom(context):
        await asyncio.sleep(0)
        raise RuntimeError("异步炸了")

    tasks, clock = [], FakeClock()

    async def scenario():
        router = make_router(tasks, clock, handler=boom)
        router.handle_event(gift("小明", 1, 1.0))
        await router.drain()

    asyncio.run(scenario())
    assert len(tasks) == 1
    assert tasks[0].text == "哈哈这个咱们直播间聊哈"
    assert tasks[0].source == "gift"
