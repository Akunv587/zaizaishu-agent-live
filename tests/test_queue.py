"""PrioritySpeakQueue 的单元测试（PROMPTS 步骤 2.5）。

验收口径：P0 插队先出；过期任务被丢弃不返回；put_idle 在有 P1 积压时返回 False；
pause 清空后 get 挂起、resume 后恢复。
"""
import asyncio

import pytest

from hub.events import SpeakTask
from hub.queue import PrioritySpeakQueue


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


def task(priority, source="chat", event_ts=1000.0, text="话术", interrupt=False):
    return SpeakTask(priority=priority, text=text, event_ts=event_ts,
                     source=source, interrupt=interrupt)


def make_queue(clock, stale=30, stale_p1=None):
    # stale_p1 缺省时与 stale 同值，保持旧语义（两级同阈值）不影响既有用例
    cfg = {"events": {"chat": {"stale_drop_s": stale,
                               "stale_drop_p1_s": stale if stale_p1 is None else stale_p1}}}
    return PrioritySpeakQueue(config=cfg, clock=clock)


def test_p0_interrupt_jumps_ahead():
    got = []

    async def scenario():
        clock = FakeClock()
        q = make_queue(clock)
        await q.put(task(1, source="chat", text="互动"))       # P1 尾插
        await q.put(task(2, source="schedule", text="口播"))    # P2 尾插
        await q.put(task(0, source="gift", text="礼物", interrupt=True))  # P0 头插
        for _ in range(3):
            got.append(await q.get())

    asyncio.run(scenario())
    assert [t.text for t in got] == ["礼物", "互动", "口播"]


def test_p0_fifo_without_interrupt():
    """无 interrupt 标记的 P0 按到达顺序出队。"""
    got = []

    async def scenario():
        clock = FakeClock()
        q = make_queue(clock)
        await q.put(task(0, text="礼物一"))
        await q.put(task(0, text="礼物二"))
        got.append(await q.get())
        got.append(await q.get())

    asyncio.run(scenario())
    assert [t.text for t in got] == ["礼物一", "礼物二"]


def test_put_head_explicit():
    got = []

    async def scenario():
        clock = FakeClock()
        q = make_queue(clock)
        await q.put(task(1, text="先来的"))
        await q.put_head(task(1, text="插队的"))
        got.append(await q.get())
        got.append(await q.get())

    asyncio.run(scenario())
    assert [t.text for t in got] == ["插队的", "先来的"]


def test_stale_task_dropped_not_returned():
    got = []

    async def scenario():
        clock = FakeClock(1000.0)
        q = make_queue(clock, stale=30)
        await q.put(task(1, event_ts=1000.0, text="过期任务"))
        clock.advance(31)  # 年龄 31 秒 > 30 秒
        await q.put(task(1, event_ts=clock.now, text="新鲜任务"))
        t = await asyncio.wait_for(q.get(), timeout=2)
        got.append(t.text)

    asyncio.run(scenario())
    # 过期任务被丢弃：get 只返回新鲜任务（过期任务不返回）
    assert got == ["新鲜任务"]


def test_stale_drop_counted_in_stats():
    from hub.stats import StatsCollector
    stats = StatsCollector()

    async def scenario():
        clock = FakeClock(1000.0)
        q = PrioritySpeakQueue(config={"events": {"chat": {"stale_drop_s": 30}}},
                               stats=stats, clock=clock)
        await q.put(task(1, event_ts=1000.0, text="过期"))
        clock.advance(100)
        await q.put(task(1, event_ts=clock.now, text="新鲜"))
        await asyncio.wait_for(q.get(), timeout=2)
        # 过期任务被丢弃后队列空，get 挂起——直接断言统计

    asyncio.run(asyncio.wait_for(scenario(), timeout=3))
    assert stats.get("dropped_stale") == 1


def test_p1_reply_survives_beyond_generic_threshold():
    """P1 弹幕回复走 stale_drop_p1_s（默认 90s）：31s 时仍播出，不被 30s 通用阈值误杀。"""

    async def scenario():
        clock = FakeClock(1000.0)
        q = PrioritySpeakQueue(config={"events": {"chat": {"stale_drop_s": 30}}},
                               clock=clock)
        await q.put(task(1, event_ts=1000.0, text="迟到但未死的回复"))
        clock.advance(31)   # 超 P0/P2 阈值 30s，未及 P1 阈值 90s
        t = await asyncio.wait_for(q.get(), timeout=2)
        assert t.text == "迟到但未死的回复"

    asyncio.run(scenario())


def test_p1_reply_dropped_after_its_own_threshold():
    """P1 超过 stale_drop_p1_s（默认 90s）才丢弃。"""

    async def scenario():
        clock = FakeClock(1000.0)
        q = PrioritySpeakQueue(config={"events": {"chat": {"stale_drop_s": 30}}},
                               clock=clock)
        await q.put(task(1, event_ts=1000.0, text="过期回复"))
        clock.advance(91)
        await q.put(task(1, event_ts=clock.now, text="新鲜回复"))
        t = await asyncio.wait_for(q.get(), timeout=2)
        assert t.text == "新鲜回复"

    asyncio.run(scenario())


def test_p0_keeps_generic_threshold():
    """P0 礼物不受 P1/P2 放宽影响：仍按 stale_drop_s 30s 过期（感谢要趁热）。"""

    async def scenario():
        clock = FakeClock(1000.0)
        q = PrioritySpeakQueue(config={"events": {"chat": {"stale_drop_s": 30}}},
                               clock=clock)
        await q.put(task(0, event_ts=1000.0, source="gift", text="过期礼物"))
        clock.advance(31)
        await q.put(task(0, event_ts=clock.now, source="gift", text="新鲜礼物"))
        t = await asyncio.wait_for(q.get(), timeout=2)
        assert t.text == "新鲜礼物"

    asyncio.run(scenario())


def test_p2_topic_survives_beyond_generic_threshold():
    """P2 话题口播走 stale_drop_p2_s（默认 180s）：31s 时仍播出——P2 让位积压
    上限 1 条，需挨过当前在播的最长话题，不能被 30s 通用阈值误杀。"""

    async def scenario():
        clock = FakeClock(1000.0)
        q = PrioritySpeakQueue(config={"events": {"chat": {"stale_drop_s": 30}}},
                               clock=clock)
        await q.put(task(2, event_ts=1000.0, source="schedule", text="迟到但未死的话题"))
        clock.advance(31)   # 超 P0 阈值 30s，未及 P2 阈值 180s
        t = await asyncio.wait_for(q.get(), timeout=2)
        assert t.text == "迟到但未死的话题"

    asyncio.run(scenario())


def test_p2_topic_dropped_after_its_own_threshold():
    """P2 超过 stale_drop_p2_s（默认 180s）才丢弃。"""

    async def scenario():
        clock = FakeClock(1000.0)
        q = PrioritySpeakQueue(config={"events": {"chat": {"stale_drop_s": 30}}},
                               clock=clock)
        await q.put(task(2, event_ts=1000.0, source="schedule", text="过期话题"))
        clock.advance(181)
        await q.put(task(2, event_ts=clock.now, source="schedule", text="新鲜话题"))
        t = await asyncio.wait_for(q.get(), timeout=2)
        assert t.text == "新鲜话题"

    asyncio.run(scenario())


def test_put_idle_yields_when_p2_self_backlog():
    """P2 自查（防口播洪水根因修复）：口播间隔小于单条播放时长时，若 P2 自身
    仍有积压，本轮必须弃轮——否则 P2 无限积压、同一话题几分钟内反复播。"""

    async def scenario():
        clock = FakeClock()
        q = make_queue(clock)
        assert await q.put_idle(task(2, source="schedule", text="口播A")) is True
        # P2 已积压 1 条：后续口播轮次全部让位，直到该条被取走
        assert await q.put_idle(task(2, source="schedule", text="口播B")) is False
        assert await q.put_idle(task(2, source="schedule", text="口播C")) is False
        assert q.qstat()["p2"] == 1
        t = await asyncio.wait_for(q.get(), timeout=2)
        assert t.text == "口播A"
        assert await q.put_idle(task(2, source="schedule", text="口播D")) is True

    asyncio.run(scenario())


def test_put_idle_yields_when_p1_backlog():
    results = []

    async def scenario():
        clock = FakeClock()
        q = make_queue(clock)
        await q.put(task(1, text="P1 积压"))
        results.append(await q.put_idle(task(2, source="schedule", text="口播")))
        results.append(await q.put_idle(task(2, source="watchdog", text="兜底")))
        assert q.qstat() == {"p0": 0, "p1": 1, "p2": 0, "total": 1}
        # 清空 P1 后口播可入队
        t = await asyncio.wait_for(q.get(), timeout=2)
        assert t.text == "P1 积压"
        results.append(await q.put_idle(task(2, source="schedule", text="口播")))
        assert q.qstat()["p2"] == 1

    asyncio.run(scenario())
    assert results == [False, False, True]


def test_pause_clears_and_suspends_get():
    got = []
    state = {}

    async def scenario():
        clock = FakeClock()
        q = make_queue(clock)
        await q.put(task(0, text="礼物", interrupt=True))
        await q.put(task(1, text="互动"))
        await q.pause()
        state["after_pause"] = q.qstat()
        state["paused"] = q.paused

        async def try_get():
            got.append(await asyncio.wait_for(q.get(), timeout=0.3))

        getter = asyncio.create_task(try_get())
        await asyncio.sleep(0.1)
        assert not getter.done()  # get 挂起
        await q.resume()
        # 恢复后队列已空，get 重新挂起等待新任务
        await q.put(task(1, event_ts=clock.now, text="恢复后新任务"))
        try:
            await asyncio.wait_for(getter, timeout=2)
        except asyncio.TimeoutError:
            state["still_suspended"] = False

    asyncio.run(scenario())
    assert state["after_pause"] == {"p0": 0, "p1": 0, "p2": 0, "total": 0}
    assert state["paused"] is True
    assert [t.text for t in got] == ["恢复后新任务"]


def test_get_waits_until_task_arrives():
    got = []

    async def scenario():
        clock = FakeClock()
        q = make_queue(clock)

        async def producer():
            await asyncio.sleep(0.05)
            await q.put(task(1, event_ts=clock.now, text="迟到任务"))

        producer_task = asyncio.create_task(producer())
        got.append(await asyncio.wait_for(q.get(), timeout=2))
        await producer_task

    asyncio.run(scenario())
    assert [t.text for t in got] == ["迟到任务"]


# ---------- v4 / M3：智能体模式 no_expire（DESIGN §8.1；V3.5 / V3.6） ----------

def test_no_expire_keeps_stale_tasks():
    """V3.5：agent 模式下队列不丢过期任务——超期 10 倍的 P0 照样播出，且不计数。"""
    from hub.stats import StatsCollector
    stats = StatsCollector()
    got = []

    async def scenario():
        clock = FakeClock(1000.0)
        q = PrioritySpeakQueue(config={"events": {"chat": {"stale_drop_s": 30}}},
                               stats=stats, clock=clock, no_expire=True)
        await q.put(task(0, event_ts=1000.0, source="gift", text="超期礼物"))
        await q.put(task(1, event_ts=1000.0, text="超期互动"))
        await q.put(task(2, event_ts=1000.0, source="schedule", text="超期口播"))
        clock.advance(3600)                        # 远超 P0/P1/P2 任一阈值
        for _ in range(3):
            got.append(await asyncio.wait_for(q.get(), timeout=2))

    asyncio.run(scenario())
    assert [t.text for t in got] == ["超期礼物", "超期互动", "超期口播"]
    assert stats.get("dropped_stale") == 0


def test_no_expire_preserves_priority_and_interrupt():
    """no_expire 只关过期判定：优先级排序与 interrupt 头插照旧生效。"""
    got = []

    async def scenario():
        clock = FakeClock()
        q = make_queue(clock)
        q.set_no_expire(True)
        await q.put(task(1, text="互动"))
        await q.put(task(2, source="schedule", text="口播"))
        await q.put(task(0, source="gift", text="礼物", interrupt=True))
        for _ in range(3):
            got.append(await asyncio.wait_for(q.get(), timeout=2))

    asyncio.run(scenario())
    assert [t.text for t in got] == ["礼物", "互动", "口播"]


def test_no_expire_put_idle_skips_backlog_check():
    """agent 模式下 P2 口播不再让位：有 P1 积压时 put_idle 仍返回 True。"""
    results = []

    async def scenario():
        clock = FakeClock()
        q = make_queue(clock)
        q.set_no_expire(True)
        await q.put(task(1, text="P1 积压"))
        results.append(await q.put_idle(task(2, source="schedule", text="口播A")))
        results.append(await q.put_idle(task(2, source="schedule", text="口播B")))
        assert q.qstat() == {"p0": 0, "p1": 1, "p2": 2, "total": 3}

    asyncio.run(scenario())
    assert results == [True, True]


def test_set_no_expire_toggle_back_restores_stale_drop():
    """V3.6：关掉开关后 _is_stale 恢复旧行为（同一队列对象内热切换）。"""
    got = []

    async def scenario():
        clock = FakeClock(1000.0)
        q = PrioritySpeakQueue(config={"events": {"chat": {"stale_drop_s": 30}}},
                               clock=clock)
        assert q.no_expire is False                # 出厂 OFF（V3.1）
        q.set_no_expire(True)
        assert q.no_expire is True
        await q.put(task(1, event_ts=1000.0, text="开关开时入队的过期任务"))
        clock.advance(31)
        got.append((await asyncio.wait_for(q.get(), timeout=2)).text)   # 不丢

        q.set_no_expire(False)
        assert q.no_expire is False
        clock.advance(200)                         # 现在任何优先级都已超龄
        await q.put(task(1, event_ts=1000.0, text="开关关后的过期任务"))
        await q.put(task(1, event_ts=clock.now, text="新鲜任务"))
        got.append((await asyncio.wait_for(q.get(), timeout=2)).text)   # 过期被丢

    asyncio.run(scenario())
    assert got == ["开关开时入队的过期任务", "新鲜任务"]


def test_no_expire_pause_still_clears():
    """暂停语义与模式无关：切到 agent 模式后 pause 仍清空全部积压。"""
    state = {}

    async def scenario():
        clock = FakeClock()
        q = make_queue(clock)
        q.set_no_expire(True)
        await q.put(task(0, text="礼物", interrupt=True))
        await q.put(task(1, text="互动"))
        await q.pause()
        state["after_pause"] = q.qstat()

    asyncio.run(scenario())
    assert state["after_pause"] == {"p0": 0, "p1": 0, "p2": 0, "total": 0}


def test_set_no_expire_same_value_is_noop():
    """重复设同值不写日志、不改状态（热切换回调可能被反复调用）。"""
    async def scenario():
        clock = FakeClock()
        q = make_queue(clock)
        q.set_no_expire(False)                     # 与出厂值相同
        assert q.no_expire is False
        q.set_no_expire(True)
        q.set_no_expire(True)
        assert q.no_expire is True

    asyncio.run(scenario())
