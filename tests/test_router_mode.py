"""Router 双模式（rules / agent）的单元测试（M3，DESIGN §4.1/§5.1）。

验收口径（V3.1~V3.4、V3.6）：
- 出厂 mode=rules，行为与旧版一致（V3.1）；
- set_mode 热切换，切换前把旧模式中间态一次性产出（V3.2）；
- agent 模式下 chat 不聚合、不按概率丢噪声，逐条产出单条上下文；
- agent 模式下 gift 跳过连击状态机，逐条产出；
- agent 模式下 follow / fansclub 跳过冷却与概率；
- agent 模式下输入侧敏感词闸门仍拦下弹幕（V3.4）；
- 两种模式都保留优先级与 interrupt 头插、逐类事件开关。
"""
import asyncio

import pytest

from hub.events import LiveEvent
from hub.router import MODE_AGENT, MODE_RULES, Router, normalize_mode


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SeqRandom:
    """random() 依次返回序列值，越界后返回最后一个。"""

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


def echo_builder(ctx):
    return f"回复：{ctx['content']}"


def make_router(tasks, clock, mode=MODE_RULES, random=None,
                reply_builder=echo_builder, input_filter=None, **cfg_over):
    cfg = {
        "events": {
            "chat": {"enabled": True, "merge_window_s": 3, "ignore_noise_p": 1.0},
            "gift": {"enabled": True, "combo_timeout_s": 5},
            "follow": {"enabled": True, "cooldown_s": 30, "probability": 0.6},
            "fansclub": {"enabled": True, "cooldown_s": 30, "probability": 0.8},
        },
        "safety": {"fallback_reply": "哈哈这个咱们直播间聊哈"},
    }
    cfg.update(cfg_over)
    return Router(tasks.append, config=cfg, clock=clock,
                  random=random or SeqRandom([0.5]),
                  reply_builder=reply_builder, input_filter=input_filter, mode=mode)


def chat(user, content, ts):
    return LiveEvent(type="chat", user=user, content=content, ts=ts)


def gift(user, count, ts, name="小心意", price=100, combo_end=False):
    return LiveEvent(type="gift", user=user, gift_name=name, gift_count=count,
                     gift_value=price, ts=ts, combo_end=combo_end)


def follow(user, ts):
    return LiveEvent(type="follow", user=user, ts=ts)


def fansclub(user, ts, content=""):
    return LiveEvent(type="fansclub", user=user, content=content, ts=ts)


# ---------- 模式归一化与出厂默认 ----------

def test_normalize_mode_falls_back_to_rules():
    """非法值一律回落 rules（安全方向：不会因配置写错就把直播交给智能体）。"""
    assert normalize_mode("agent") == MODE_AGENT
    assert normalize_mode("AGENT ") == MODE_AGENT
    assert normalize_mode("rules") == MODE_RULES
    for bad in (None, "", "smart", 1, "l1"):
        assert normalize_mode(bad) == MODE_RULES


def test_default_mode_is_rules_and_behaves_old_way():
    """V3.1：出厂 rules，chat 仍聚合、噪声仍按概率丢弃。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, random=SeqRandom([0.0]))   # 噪声必丢
    assert router.mode == MODE_RULES

    router.handle_event(chat("甲", "这游戏怎么下载", 0))
    router.handle_event(chat("乙", "这游戏怎么下载啊", 0.5))       # 相似 → 合并
    router.handle_event(chat("丙", "!!", 1.0))                    # 噪声 + random 0.0 → 丢
    assert tasks == []                                            # 窗口未到期，无产出
    assert router.has_pending_reply() is True

    clock.advance(3.5)
    router.scan()
    assert len(tasks) == 1
    assert tasks[0].merged_count == 2                             # 两条相似合并成一组
    assert tasks[0].text == "回复：这游戏怎么下载"


# ---------- agent 模式：chat ----------

def test_agent_mode_chat_no_merge_no_noise_drop():
    """agent 模式：逐条产出（不聚合）、噪声不按概率丢。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, mode=MODE_AGENT, random=SeqRandom([0.0]))

    router.handle_event(chat("甲", "这游戏怎么下载", 0))
    router.handle_event(chat("乙", "这游戏怎么下载啊", 0.1))       # 相似也不合并
    router.handle_event(chat("丙", "!!", 0.2))                    # 噪声也照收
    assert len(tasks) == 3
    assert [t.text for t in tasks] == ["回复：这游戏怎么下载", "回复：这游戏怎么下载啊", "回复：!!"]
    assert all(t.merged_count == 1 for t in tasks)
    assert router.has_pending_reply() is False                    # 没有滞留窗口


def test_agent_mode_chat_uses_agent_handler():
    """set_agent_handler 接管后，chat 上下文交给它（M4 的接缝）。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, mode=MODE_AGENT, reply_builder=None)
    seen = []

    def handler(ctx):
        seen.append(ctx)
        return {"text": "智能体的回答", "emotion": "happy", "speed": 1.2}

    router.set_agent_handler(handler)
    router.handle_event(chat("甲", "这游戏怎么下载", 0))

    assert len(seen) == 1
    assert seen[0]["content"] == "这游戏怎么下载" and seen[0]["merged_count"] == 1
    assert len(tasks) == 1
    assert (tasks[0].text, tasks[0].emotion, tasks[0].speed) == ("智能体的回答", "happy", 1.2)


def test_agent_mode_without_handler_falls_back_to_reply_builder():
    """钩子未注入（开关已开、智能体未接管）时 chat 走 reply_builder，不丢弹幕。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, mode=MODE_AGENT)
    router.handle_event(chat("甲", "在吗", 0))
    assert [t.text for t in tasks] == ["回复：在吗"]


def test_agent_handler_async_result_and_exception_fallback():
    """钩子返回 awaitable 时异步补齐；钩子抛异常时回落 reply_builder 的兜底。

    异步钩子需要运行中的事件循环（与异步 reply_builder 同约束），故整段跑在
    asyncio 里；生产路径由 main 的事件循环提供。
    """
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, mode=MODE_AGENT, reply_builder=None)

    async def handler(ctx):
        await asyncio.sleep(0)
        return "异步答案"

    async def scenario():
        router.set_agent_handler(handler)
        router.handle_event(chat("甲", "问题", 0))
        await router.drain(timeout=2)

    asyncio.run(scenario())
    assert [t.text for t in tasks] == ["异步答案"]

    def boom(ctx):
        raise RuntimeError("智能体炸了")

    router.set_agent_handler(boom)
    router.handle_event(chat("乙", "问题二", 0))
    assert tasks[-1].text == "哈哈这个咱们直播间聊哈"             # 兜底口播


def test_agent_mode_chat_still_blocked_by_input_filter():
    """V3.4：agent 模式下敏感词弹幕仍被输入侧拦下，不进智能体。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, mode=MODE_AGENT,
                         input_filter=lambda text: "加微信" in text)
    seen = []
    router.set_agent_handler(lambda ctx: seen.append(ctx))

    router.handle_event(chat("甲", "加微信私聊", 0))
    assert tasks == [] and seen == []                             # 既没产出也没进智能体

    router.handle_event(chat("乙", "这游戏好玩吗", 0))
    assert len(seen) == 1                                         # 正常弹幕照进


# ---------- agent 模式：gift / follow / fansclub ----------

def test_agent_mode_gift_skips_combo_state_machine():
    """agent 模式：每条礼物事件各自产出，不做连击合并，也不等 combo_end。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, mode=MODE_AGENT)

    for i in range(3):
        router.handle_event(gift("甲", i + 1, i * 0.1, price=100, combo_end=False))
    assert len(tasks) == 3                                        # 逐条产出，无需 combo_end
    assert [t.tier for t in tasks] == ["mid", "mid", "mid"]
    assert all(t.interrupt for t in tasks)                        # mid 档仍插队
    assert router._combos == {}                                   # 不留连击状态

    clock.advance(10)
    router.scan()                                                 # 超时扫描不应再产出
    assert len(tasks) == 3


def test_agent_mode_gift_tier_from_single_event():
    """单条礼物的分档取该事件自身的 value（低/中/高三档与插队规则不变）。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, mode=MODE_AGENT)
    router.handle_event(gift("甲", 1, 0, name="小星星", price=10))
    router.handle_event(gift("乙", 1, 0.1, name="火箭", price=5000))
    assert [(t.tier, t.interrupt) for t in tasks] == [("low", False), ("high", True)]


def test_agent_mode_follow_skips_cooldown_and_probability():
    """agent 模式：follow / fansclub 无冷却、无概率（random 不给机会也照产出）。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, mode=MODE_AGENT, random=SeqRandom([0.99]))

    for i in range(3):
        router.handle_event(follow("甲", i * 0.1))
    assert len(tasks) == 3                                        # 冷却不生效

    for i in range(2):
        router.handle_event(fansclub("乙", i * 0.1))
    assert len(tasks) == 5                                        # 概率不生效
    assert all(t.test is False for t in tasks)                    # 真实任务，不是测试事件
    assert [t.source for t in tasks] == ["follow"] * 3 + ["fansclub"] * 2


def test_rules_mode_follow_cooldown_still_applies():
    """对照：rules 模式冷却与概率照旧（0.99 >= 0.6 → 跳过）。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, random=SeqRandom([0.99]))
    router.handle_event(follow("甲", 0))
    assert tasks == []


def test_agent_mode_respects_event_enabled_switches():
    """逐类事件开关在 agent 模式下仍生效（护栏不属于模式差异）。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, mode=MODE_AGENT)
    router.set_event_enabled({"follow": False, "gift_low": False})
    router.handle_event(follow("甲", 0))
    router.handle_event(gift("乙", 1, 0, name="小星星", price=10))
    assert tasks == []


def test_agent_mode_enter_switch_unchanged():
    """enter 默认关闭；注入存档开关开启后两种模式都产出。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, mode=MODE_AGENT)
    router.handle_event(LiveEvent(type="enter", user="甲", ts=0))
    assert tasks == []
    router.set_event_enabled({"enter": True})
    router.handle_event(LiveEvent(type="enter", user="乙", ts=0.1))
    assert [t.text for t in tasks] == ["[待生成]欢迎 乙"]


# ---------- 热切换 ----------

def test_set_mode_flushes_intermediate_state():
    """V3.2：切到 agent 前把聚合窗口与未结算连击一次性产出，不留半截状态。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock)
    router.handle_event(chat("甲", "在吗", 0))                     # 窗口打开
    router.handle_event(gift("乙", 3, 0, name="小心意", price=100, combo_end=False))
    assert tasks == []

    assert router.set_mode(MODE_AGENT) == MODE_AGENT
    assert len(tasks) == 2                                        # 窗口 1 条 + 连击 1 条
    assert sorted(t.source for t in tasks) == ["chat", "gift"]
    assert router._window is None and router._combos == {}

    # 切换后再来弹幕：走 agent 单条路径
    router.handle_event(chat("丙", "在吗", 0.2))
    assert len(tasks) == 3 and tasks[-1].merged_count == 1


def test_set_mode_same_value_is_noop_and_returns_mode():
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, mode=MODE_AGENT)
    router.handle_event(chat("甲", "在吗", 0))                     # 已产出，无中间态
    assert router.set_mode("agent") == MODE_AGENT
    assert router.set_mode("weird") == MODE_RULES                # 非法值回落，且真的切了
    assert router.mode == MODE_RULES
    assert len(tasks) == 1


def test_switch_back_to_rules_restores_aggregation():
    """V3.6（router 侧）：切回 rules 后聚合与噪声概率恢复旧行为。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, random=SeqRandom([0.0]))
    router.set_mode(MODE_AGENT)
    router.handle_event(chat("甲", "这游戏怎么下载", 0))
    assert len(tasks) == 1 and tasks[0].merged_count == 1

    router.set_mode(MODE_RULES)
    router.handle_event(chat("乙", "这游戏怎么下载", 0.1))
    router.handle_event(chat("丙", "这游戏怎么下载啊", 0.2))
    clock.advance(3.5)
    router.scan()
    assert len(tasks) == 2 and tasks[-1].merged_count == 2


def test_pause_applies_in_both_modes():
    """F9 暂停语义与模式无关：agent 模式下事件照收、只记录不产出。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, mode=MODE_AGENT)
    router.set_paused(True)
    router.handle_event(chat("甲", "在吗", 0))
    router.handle_event(gift("乙", 1, 0, price=5000))
    assert tasks == []
    router.set_paused(False)
    router.handle_event(chat("丙", "在吗", 0.1))
    assert [t.text for t in tasks] == ["回复：在吗"]
