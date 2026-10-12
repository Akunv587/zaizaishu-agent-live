"""Router follow/fansclub 冷却与概率、enter 开关的单元测试（PROMPTS 步骤 2.3）。

验收口径：冷却期内 5 个关注只产出 1 个任务且 user 为最后一位；
probability 设为 0 时不产出；fansclub 与 follow 的冷却互不影响。
"""
import asyncio

import pytest

from hub.events import LiveEvent
from hub.router import Router
from tests.test_router_chat import FakeClock, SeqRandom


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


def make_router(tasks, clock, random, follow_p=0.6, fansclub_p=0.8, cooldown=30,
                enter_enabled=False):
    cfg = {
        "events": {
            "follow": {"enabled": True, "cooldown_s": cooldown, "probability": follow_p},
            "fansclub": {"enabled": True, "cooldown_s": cooldown, "probability": fansclub_p},
            "enter": {"enabled": enter_enabled},
        }
    }
    return Router(tasks.append, config=cfg, clock=clock, random=random)


def follow(user, ts):
    return LiveEvent(type="follow", user=user, ts=ts)


def fansclub(user, ts, content=""):
    return LiveEvent(type="fansclub", user=user, content=content, ts=ts)


def drain(router):
    asyncio.run(router.drain(timeout=2))


def test_five_follows_in_cooldown_emit_one_with_last_user():
    tasks, clock = [], FakeClock(0.0)
    # 第 1 个随机数用于首个关注（0.9 >= 0.6 → 概率未中，不产出但开启冷却），
    # 第 2 个用于冷却结束后积压补播（0.0 < 0.6 → 产出）
    router = make_router(tasks, clock, SeqRandom([0.9, 0.0]))

    router.handle_event(follow("首个", 0))       # 概率未中，冷却开始
    assert tasks == []

    for i in range(5):
        router.handle_event(follow(f"观众{i + 1}", 1 + i))  # 冷却期内积压
    assert tasks == []

    clock.advance(31.0)
    router.scan()
    drain(router)
    assert len(tasks) == 1
    assert "观众5" in tasks[0].text  # 取最近一位
    assert tasks[0].source == "follow"
    assert tasks[0].priority == 1


def test_first_follow_emits_when_probability_hit():
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, SeqRandom([0.0]))
    router.handle_event(follow("甲", 0))
    drain(router)
    assert len(tasks) == 1
    assert "甲" in tasks[0].text


def test_probability_zero_never_emits():
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, SeqRandom([0.0]), follow_p=0.0)
    router.handle_event(follow("甲", 0))
    drain(router)
    clock.advance(35)
    router.handle_event(follow("乙", 35))
    clock.advance(35)
    router.scan()
    drain(router)
    assert tasks == []


def test_follow_and_fansclub_cooldowns_independent():
    tasks, clock = [], FakeClock(0.0)
    # 随机序列：follow 概率未中(0.9)，fansclub 命中(0.0)，follow 积压补播命中(0.0)
    router = make_router(tasks, clock, SeqRandom([0.9, 0.0, 0.0]))

    router.handle_event(follow("关注者", 0))     # follow 冷却开始（概率未中，不产出）
    assert tasks == []
    router.handle_event(fansclub("粉丝团员", 1))  # fansclub 独立冷却 → 直接产出
    drain(router)
    assert len(tasks) == 1
    assert tasks[0].source == "fansclub"

    # follow 冷却期内积压两位；fansclub 冷却期内也积压——两者互不干扰
    router.handle_event(follow("关注者2", 2))
    router.handle_event(follow("关注者3", 3))
    router.handle_event(fansclub("粉丝团员2", 4))
    assert tasks == [] or all(t.source == "fansclub" for t in tasks)

    clock.advance(31.0)
    router.scan()
    drain(router)
    sources = [t.source for t in tasks]
    assert sources.count("fansclub") == 2  # 首个 + 冷却结束积压补播
    assert sources.count("follow") == 1    # 冷却结束取最近一位补播
    assert "关注者3" in tasks[-1].text if tasks[-1].source == "follow" else True


def test_enter_disabled_by_default():
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, SeqRandom([0.0]))
    router.handle_event(LiveEvent(type="enter", user="进场者", ts=0))
    clock.advance(5)
    router.scan()
    drain(router)
    assert tasks == []


def test_test_follow_bypasses_cooldown_and_probability():
    """测试关注（test 标记）：绕过冷却+概率，即使概率为 0 也稳定产出。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, SeqRandom([0.0]), follow_p=0.0)
    router.handle_event(follow("甲", 0))            # 普通关注：概率 0，不产出
    drain(router)
    assert tasks == []
    router.handle_event(LiveEvent(type="follow", user="测试观众", ts=1, test=True))
    drain(router)
    assert len(tasks) == 1
    assert tasks[0].source == "follow"
    assert tasks[0].test is True
    assert "测试观众" in tasks[0].text


def test_test_enter_bypasses_disabled_switch():
    """测试进场（test 标记）：即使 enter 默认关闭也产出。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, SeqRandom([0.0]), enter_enabled=False)
    router.handle_event(LiveEvent(type="enter", user="进场者", ts=0))   # 普通进场：关闭，不产出
    drain(router)
    assert tasks == []
    router.handle_event(LiveEvent(type="enter", user="测试观众", ts=1, test=True))
    drain(router)
    assert len(tasks) == 1
    assert tasks[0].source == "enter"
    assert tasks[0].test is True


def test_event_switch_disables_follow():
    """事件模板库开关：关闭关注后即使概率必中也静默。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, SeqRandom([0.0]), follow_p=1.0)
    router.set_event_enabled({"follow": False})
    router.handle_event(follow("甲", 0))
    drain(router)
    assert tasks == []


def test_event_switch_enables_enter():
    """事件模板库开关：开启进场后（config enter 仍默认关）也能产出。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, SeqRandom([0.0]))
    router.set_event_enabled({"enter": True})
    router.handle_event(LiveEvent(type="enter", user="进场者", ts=0))
    drain(router)
    assert len(tasks) == 1
    assert tasks[0].source == "enter"


def test_fansclub_input_filter_on_content():
    tasks, clock = [], FakeClock(0.0)
    cfg = {"events": {"fansclub": {"enabled": True, "cooldown_s": 30, "probability": 1.0}}}
    router = Router(tasks.append, config=cfg, clock=clock, random=SeqRandom([0.0]),
                    input_filter=lambda text: "敏感" in text)
    router.handle_event(fansclub("甲", 0, content="带敏感词的附言"))
    clock.advance(31)
    router.scan()
    drain(router)
    assert tasks == []


def test_paused_router_records_but_not_emits():
    """暂停抑制（PROMPTS 步骤 4.4 验收：暂停期间任务产出抑制）。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, SeqRandom([0.0]))
    router.set_paused(True)
    router.handle_event(follow("甲", 0))
    router.handle_event(fansclub("乙", 1))
    clock.advance(31)
    router.scan()
    drain(router)
    assert tasks == []

    router.set_paused(False)
    router.handle_event(follow("丙", 40))
    drain(router)
    assert len(tasks) == 1
    assert "丙" in tasks[0].text
