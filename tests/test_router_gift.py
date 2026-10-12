"""Router gift 连击状态机的单元测试（PROMPTS 步骤 2.4）。

验收口径：连击序列 count 1、2、3 加结束标记只结算一次且 count=3；
无结束标记时 5 秒超时结算；不同用户或不同礼物互不干扰。
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


def make_router(tasks, clock, combo_timeout=5):
    cfg = {"events": {"gift": {"enabled": True, "combo_timeout_s": combo_timeout}}}
    return Router(tasks.append, config=cfg, clock=clock, random=SeqRandom([0.0]))


def gift(user, count, ts, name="小心意", price=100, combo_end=False):
    return LiveEvent(type="gift", user=user, content="", ts=ts,
                     gift_name=name, gift_count=count, gift_value=price,
                     combo_end=combo_end)


def drain(router):
    asyncio.run(router.drain(timeout=2))


def test_combo_with_end_marker_settles_once():
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock)

    router.handle_event(gift("甲", 1, 0, combo_end=False))
    router.handle_event(gift("甲", 2, 1, combo_end=False))
    assert tasks == []  # 连击进行中不结算
    router.handle_event(gift("甲", 3, 2, combo_end=True))
    drain(router)

    assert len(tasks) == 1
    task = tasks[0]
    assert task.priority == 0
    assert task.source == "gift"
    assert "甲" in task.text
    assert "小心意" in task.text
    assert "3" in task.text  # 结算 count=3
    # price=100 → mid 档 → 插队
    assert task.tier == "mid"
    assert task.interrupt is True


def test_combo_timeout_settles_without_end_marker():
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, combo_timeout=5)

    router.handle_event(gift("乙", 2, 0, combo_end=False))
    clock.advance(4.9)
    router.scan()
    drain(router)
    assert tasks == []  # 未超时

    clock.advance(0.2)  # 距最后一条超过 5 秒
    router.scan()
    drain(router)
    assert len(tasks) == 1
    assert "2" in tasks[0].text
    assert tasks[0].tier == "mid"


def test_combo_end_then_more_events_no_leak():
    """结算后状态表清空：同用户同礼物再来一笔是新的连击。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock)

    router.handle_event(gift("甲", 3, 0, combo_end=True))
    drain(router)
    assert len(tasks) == 1

    router.handle_event(gift("甲", 1, 10, combo_end=True))
    drain(router)
    assert len(tasks) == 2
    assert "1" in tasks[1].text


def test_different_users_and_gifts_independent():
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock)

    router.handle_event(gift("甲", 2, 0, name="小心意", combo_end=False))
    router.handle_event(gift("乙", 5, 0.5, name="小心意", combo_end=False))   # 不同用户
    router.handle_event(gift("甲", 7, 1.0, name="大火箭", price=2000, combo_end=False))  # 不同礼物
    clock.advance(6)
    router.scan()
    drain(router)

    assert len(tasks) == 3
    texts = sorted(t.text for t in tasks)
    assert any("5" in t for t in texts)
    assert any("7" in t for t in texts)
    tiers = {t.tier for t in tasks}
    assert "high" in tiers  # 大火箭 2000
    assert "low" not in tiers  # 小心意 100 → mid


def test_tier_thresholds():
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock)

    router.handle_event(gift("低", 1, 0, name="A", price=99, combo_end=True))
    router.handle_event(gift("中", 1, 0.1, name="B", price=100, combo_end=True))
    router.handle_event(gift("高", 1, 0.2, name="C", price=1000, combo_end=True))
    drain(router)

    by_tier = {t.tier: t for t in tasks}
    assert set(by_tier) == {"low", "mid", "high"}
    assert by_tier["low"].interrupt is False
    assert by_tier["mid"].interrupt is True
    assert by_tier["high"].interrupt is True
    assert all(t.priority == 0 for t in tasks)


def test_event_switch_disables_gift_tier():
    """事件模板库开关：关闭大礼物后该档静默，其余档不受影响。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock)
    router.set_event_enabled({"gift_high": False})
    router.handle_event(gift("甲", 1, 0, price=2000, combo_end=True))  # high 档被关闭
    drain(router)
    assert tasks == []
    router.handle_event(gift("乙", 1, 1, price=100, combo_end=True))   # mid 档仍生效
    drain(router)
    assert len(tasks) == 1
    assert tasks[0].tier == "mid"


def test_settle_event_ts_is_settle_time():
    tasks, clock = [], FakeClock(100.0)
    router = make_router(tasks, clock)
    router.handle_event(gift("甲", 3, 100, combo_end=False))
    clock.advance(5.5)
    router.scan()  # 超时结算，event_ts 取结算时刻
    drain(router)
    assert tasks[0].event_ts == 105.5
