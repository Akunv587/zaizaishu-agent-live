"""看门狗失败计数告警的单元测试（PROMPTS 步骤 5.2）。

验收口径：注入假时钟/假 sleep/假通知——失败计数超阈值汇总通知并复位
（下一批重新累计才再报），未达阈值不报。

FakeSleep 语义：第 n 次睡眠结束→on_wake(n)→若 n≥stop_after 置位 stop→
run 在检查前检查 stop，故 stop_after=N 时实际执行 N-1 次巡检。
"""
import asyncio

import pytest

from hub.stats import StatsCollector
from hub.watchdog import Watchdog

INTERVAL = 30.0


class FakeClock:
    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, dt: float):
        self.t += dt


class FakeSleep:
    """睡眠即时间流逝：推进假时钟；on_wake(n) 在第 n 次睡眠结束、第 n 次
    检查前回调（供测试在巡检边界注入状态）。"""

    def __init__(self, clock, stop=None, stop_after=None, on_wake=None):
        self.clock = clock
        self.stop = stop
        self.stop_after = stop_after
        self.on_wake = on_wake
        self.delays = []
        self.count = 0

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.count += 1
        self.clock.advance(delay)
        if self.on_wake is not None:
            self.on_wake(self.count)
        if self.stop is not None and self.count >= self.stop_after:
            self.stop.set()


class FakeQueue:
    def __init__(self, put_ok=True):
        self.paused = False
        self.tasks = []
        self.put_ok = put_ok

    async def put_idle(self, task) -> bool:
        self.tasks.append(task)
        return self.put_ok


class FakeNotify:
    def __init__(self):
        self.calls = []

    def __call__(self, title, message):
        self.calls.append((title, message))


class FakeLog:
    def __init__(self):
        self.events = []

    def _record(self, name, extra=None):
        self.events.append((name, extra))

    def info(self, name, extra=None):
        self._record(name, extra)

    def warning(self, name, extra=None):
        self._record(name, extra)

    def debug(self, name, extra=None):
        self._record(name, extra)

    def error(self, name, extra=None):
        self._record(name, extra)


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod

    logger_mod.setup(tmp_path, "warning")
    return tmp_path


def make_watchdog(clock, sleep, notify, stats=None, fail_threshold=10):
    queue = FakeQueue()
    stats = stats if stats is not None else StatsCollector()
    wd = Watchdog(queue, stats=stats,
                  config={"watchdog": {"fail_threshold": fail_threshold}},
                  log=FakeLog(), clock=clock, sleep=sleep,
                  notify=notify, interval_s=INTERVAL)
    return wd, queue, stats


def test_fail_counters_alert_and_reset():
    """llm_fail 达阈值：汇总通知一次并复位；重新累计到阈值才再报。"""
    clock, notify = FakeClock(0.0), FakeNotify()
    stop = asyncio.Event()
    stats = StatsCollector()
    stats.inc("llm_fail", 10)
    stats.inc("tts_fail", 2)  # 未达阈值不应入通知

    def on_wake(count):
        if count == 3:  # 第 2 次检查（无告警）后再累计 10 次，第 3 次检查应再报
            stats.inc("llm_fail", 10)

    sleep = FakeSleep(clock, stop, stop_after=4, on_wake=on_wake)  # 3 次检查
    wd, _, _ = make_watchdog(clock, sleep, notify, stats=stats, fail_threshold=10)
    asyncio.run(wd.run(stop))
    assert len(notify.calls) == 2
    assert "LLM失败" in notify.calls[0][1] and "TTS失败" not in notify.calls[0][1]
    assert stats.get("llm_fail") == 0  # 第 3 轮告警已复位 on_wake 注入的 10
    assert stats.get("tts_fail") == 2  # 未达阈值计数保持
    assert wd.stats()["fail_alerts"] == 2


def test_both_fail_counters_one_notification():
    """同轮检查 llm_fail 与 tts_fail 同时超阈值：汇总为一条通知。"""
    clock, notify = FakeClock(0.0), FakeNotify()
    stop = asyncio.Event()
    stats = StatsCollector()
    stats.inc("llm_fail", 6)
    stats.inc("tts_fail", 7)
    sleep = FakeSleep(clock, stop, stop_after=2)  # 1 次检查
    wd, _, _ = make_watchdog(clock, sleep, notify, stats=stats, fail_threshold=5)
    asyncio.run(wd.run(stop))
    assert len(notify.calls) == 1
    assert "LLM失败" in notify.calls[0][1] and "TTS失败" in notify.calls[0][1]
    assert stats.get("llm_fail") == 0 and stats.get("tts_fail") == 0


def test_fail_below_threshold_no_alert():
    clock, notify = FakeClock(0.0), FakeNotify()
    stop = asyncio.Event()
    stats = StatsCollector()
    stats.inc("llm_fail", 9)
    sleep = FakeSleep(clock, stop, stop_after=2)  # 1 次检查
    wd, _, _ = make_watchdog(clock, sleep, notify, stats=stats, fail_threshold=10)
    asyncio.run(wd.run(stop))
    assert notify.calls == []
    assert stats.get("llm_fail") == 9  # 不复位