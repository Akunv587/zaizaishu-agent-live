"""定时口播调度的单元测试（PROMPTS 步骤 5.1）。

验收口径：注入假时钟/假随机/假 sleep——两次口播触发且轮换不重复；
interval ±60s 抖动；P1 占用时 put_idle 返回 False 且不产生积压；
暂停期间跳过；enabled=false 与空话术池不产出。
"""
import asyncio
import time

import pytest

from hub.events import SpeakTask
from hub.scheduler import Scheduler
from hub.stats import StatsCollector


class FakeRandom:
    def __init__(self, jitter=0.0):
        self.jitter = jitter

    def uniform(self, a, b):
        return self.jitter


class FakeSleep:
    """记录等待时长；第 n 次等待后置位 stop（控制轮数）。"""

    def __init__(self, stop: asyncio.Event = None, stop_after: int = None):
        self.delays = []
        self.stop = stop
        self.stop_after = stop_after
        self.count = 0

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.count += 1
        if self.stop is not None and self.count >= self.stop_after:
            self.stop.set()


class FakeQueue:
    def __init__(self, results=None, on_put=None):
        self.paused = False
        self.tasks = []
        self._results = list(results or [])
        self._on_put = on_put

    async def put_idle(self, task) -> bool:
        self.tasks.append(task)
        if self._on_put is not None:
            self._on_put(self)
        return self._results.pop(0) if self._results else True


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


def make_config(pool, interval_min=7, enabled=True):
    return {"schedule": {"enabled": enabled, "interval_min": interval_min, "pool": pool}}


def test_two_fires_with_rotation_and_no_repeat():
    """两次口播触发：等待间隔正确、两条话术不同、任务为 P2/schedule。"""
    stop = asyncio.Event()
    queue = FakeQueue(on_put=lambda q: len(q.tasks) >= 2 and stop.set())
    sleep = FakeSleep()
    stats = StatsCollector()
    sched = Scheduler(queue, config=make_config(["甲", "乙", "丙"]),
                      stats=stats, random=FakeRandom(0.0), sleep=sleep)
    sched.set_live(True)
    asyncio.run(sched.run(stop))
    assert sleep.delays == [7 * 60, 7 * 60]  # 无抖动时恰为 interval_min 分钟
    assert len(queue.tasks) == 2
    assert queue.tasks[0].text != queue.tasks[1].text
    assert all(t.priority == 2 and t.source == "schedule" for t in queue.tasks)
    assert stats.get("idle_announced") == 2
    assert stats.get("task_produced") == 2  # 口播入队计入统一产出口径
    assert sched.stats() == {"announced": 2, "yielded": 0}


def test_interval_jitter_applied():
    """±60 秒抖动进入等待时长。"""
    stop = asyncio.Event()
    queue = FakeQueue()
    sleep = FakeSleep(stop, stop_after=1)
    sched = Scheduler(queue, config=make_config(["甲"], interval_min=5),
                      random=FakeRandom(30.0), sleep=sleep)
    sched.set_live(True)
    asyncio.run(sched.run(stop))
    assert sleep.delays == [5 * 60 + 30]


def test_next_text_avoids_recent_two():
    """轮换避开最近播过的 2 条：指针指向已用条目时顺延到未用过的一条。"""
    queue = FakeQueue()
    sched = Scheduler(queue, config=make_config(["A", "B", "C", "D"]))

    async def picks(n):
        for _ in range(n):
            await sched._emit_once()

    asyncio.run(picks(3))
    # 已播 A、B、C（recent=[B,C]）；指针拨回 1（B）：第 4 次应跳过 B、C 取 D
    sched._ptr = 1
    asyncio.run(picks(1))
    texts = [t.text for t in queue.tasks]
    assert texts == ["A", "B", "C", "D"]
    # 池全覆盖且任意连续 3 条互不重复
    for i in range(len(texts) - 2):
        assert len(set(texts[i:i + 3])) == 3


def test_put_idle_yields_when_busy_no_backlog():
    """P0/P1 积压时让位：不入队、计数 yielded，队列不产生 P2 积压。"""
    from hub.queue import PrioritySpeakQueue

    queue = PrioritySpeakQueue(config={})
    stop = asyncio.Event()
    sleep = FakeSleep(stop, stop_after=3)  # 第 3 次等待置位停止 → 两次口播尝试
    stats = StatsCollector()
    sched = Scheduler(queue, config=make_config(["甲", "乙", "丙"]),
                      stats=stats, random=FakeRandom(0.0), sleep=sleep)
    sched.set_live(True)

    async def scenario():
        await queue.put(SpeakTask(priority=1, text="互动回复", event_ts=0, source="chat"))
        await sched.run(stop)

    asyncio.run(scenario())
    assert queue.qstat() == {"p0": 0, "p1": 1, "p2": 0, "total": 1}  # 只有原 P1，无 P2 积压
    assert stats.get("idle_yielded") == 2
    assert stats.get("idle_announced") == 0
    assert stats.get("task_produced") == 0  # 让位不入队，不计产出
    assert sched.stats() == {"announced": 0, "yielded": 2}


def test_skip_round_while_paused():
    """暂停（FR-5）期间本轮口播跳过，不产出任务。"""
    queue = FakeQueue()
    queue.paused = True
    stop = asyncio.Event()
    sleep = FakeSleep(stop, stop_after=2)
    sched = Scheduler(queue, config=make_config(["甲", "乙"]), random=FakeRandom(0.0),
                      sleep=sleep)
    sched.set_live(True)
    asyncio.run(sched.run(stop))
    assert queue.tasks == []
    assert sleep.delays == [7 * 60, 7 * 60]


def test_disabled_or_empty_pool_produces_nothing():
    stop = asyncio.Event()
    queue = FakeQueue()
    sleep = FakeSleep(stop, stop_after=1)
    sched = Scheduler(queue, config=make_config(["甲"], enabled=False), sleep=sleep)
    asyncio.run(sched.run(stop))
    assert queue.tasks == [] and sleep.delays == []

    queue2 = FakeQueue()
    sleep2 = FakeSleep(stop, stop_after=1)
    sched2 = Scheduler(queue2, config={"schedule": {"enabled": True, "interval_min": 7,
                                                    "pool": []}}, sleep=sleep2)
    asyncio.run(sched2.run(stop))
    assert queue2.tasks == [] and sleep2.delays == []


# ---------- v3.1：秒级调度 / 音频条目 / 热配置 ----------

def test_seconds_interval_and_jitter():
    """configure 的秒级间隔与抖动进入等待时长。"""
    stop = asyncio.Event()
    queue = FakeQueue()
    sleep = FakeSleep(stop, stop_after=1)
    sched = Scheduler(queue, config=make_config(["甲"]), random=FakeRandom(5.0), sleep=sleep)
    sched.configure(interval_s=30, jitter_s=15)
    sched.set_live(True)
    asyncio.run(sched.run(stop))
    assert sleep.delays == [35]


def test_audio_item_emits_task_with_audio_path():
    """带音频的话题条目：SpeakTask.audio_path 透传，text 为空也入队。"""
    queue = FakeQueue()
    sched = Scheduler(queue, config=make_config(["占位"]),
                    random=FakeRandom(0.0), sleep=FakeSleep())
    sched.configure(items=[{"text": "话题文本", "audio_path": "D:/a/topic.wav"},
                           {"text": "", "audio_path": ""},  # 空条目剔除
                           "纯字符串条目"])
    assert len(sched._items) == 2
    asyncio.run(sched._emit_once())
    task = queue.tasks[0]
    assert task.priority == 2 and task.source == "schedule"
    assert task.audio_path == "D:/a/topic.wav"
    assert task.text == "话题文本"


def test_configure_replaces_items_and_resets_rotation():
    """热换条目：轮换指针与最近记录重置，新条目立即生效。"""
    queue = FakeQueue()
    sched = Scheduler(queue, config=make_config(["A", "B"]), random=FakeRandom(0.0),
                    sleep=FakeSleep())
    asyncio.run(sched._emit_once())
    assert queue.tasks[-1].text == "A"
    sched.configure(items=[{"text": "新话题", "audio_path": ""}])
    asyncio.run(sched._emit_once())
    assert queue.tasks[-1].text == "新话题"
    assert sched._recent == ["新话题"]


def test_hot_disable_then_enable():
    """运行中热停用：空转走 1s 心跳不再产出；热恢复后按原间隔继续轮播。

    确定性驱动：sleep 钩子在指定次数上切换 enabled（全部同步，无事件循环竞态）。
    时序：c1 睡 420s→产出 T1+停用；c2/c3 空转 1s 心跳；c3 恢复；c4 睡 420s→产出 T2。
    """

    class HookSleep(FakeSleep):
        def __init__(self, hooks, **kw):
            super().__init__(**kw)
            self._hooks = hooks

        async def __call__(self, delay):
            await super().__call__(delay)
            fn = self._hooks.get(self.count)
            if fn is not None:
                fn()

    stop = asyncio.Event()
    queue = FakeQueue()
    sched = Scheduler(queue, config=make_config(["甲"]), random=FakeRandom(0.0))
    sleep = HookSleep({1: lambda: sched.configure(enabled=False),
                       3: lambda: sched.configure(enabled=True)},
                      stop=stop, stop_after=5)
    sched._sleep = sleep
    sched.set_live(True)
    asyncio.run(sched.run(stop))

    assert sleep.delays == [7 * 60, 1.0, 1.0, 7 * 60, 7 * 60]  # 停用期间 1s 心跳
    assert [t.text for t in queue.tasks] == ["甲", "甲"]  # 空转无产出，恢复后续播


def test_not_started_live_produces_nothing():
    """「开始直播」门控：未点开始直播时，话题轮播只空转 1s 心跳、不产出任务。"""
    stop = asyncio.Event()
    queue = FakeQueue()
    sleep = FakeSleep(stop, stop_after=3)
    sched = Scheduler(queue, config=make_config(["甲"]), random=FakeRandom(0.0),
                      sleep=sleep)
    asyncio.run(sched.run(stop))
    assert queue.tasks == []
    assert sleep.delays == [1.0, 1.0, 1.0]  # 全程心跳，未进入轮播等待


def test_set_live_starts_rotation():
    """set_live(True) 后才按间隔轮播：开播前 1s 心跳，开播后进入正常间隔并产出。"""

    class HookSleep(FakeSleep):
        def __init__(self, hooks, **kw):
            super().__init__(**kw)
            self._hooks = hooks

        async def __call__(self, delay):
            await super().__call__(delay)
            fn = self._hooks.get(self.count)
            if fn is not None:
                fn()

    stop = asyncio.Event()
    queue = FakeQueue()
    sched = Scheduler(queue, config=make_config(["甲"]), random=FakeRandom(0.0))
    sleep = HookSleep({1: lambda: sched.set_live(True)}, stop=stop, stop_after=3)
    sched._sleep = sleep
    asyncio.run(sched.run(stop))

    assert sleep.delays == [1.0, 7 * 60, 7 * 60]  # 开播前心跳，开播后按间隔
    assert [t.text for t in queue.tasks] == ["甲"]  # 开播后开始产出话题


def test_reply_busy_defers_next_topic():
    """回复处理中（reply_busy=True）时话题让位等待，直到回复结束才发射下一条。"""
    stop = asyncio.Event()
    queue = FakeQueue()
    sched = Scheduler(queue, config=make_config(["甲"]), random=FakeRandom(0.0))
    sched.set_live(True)
    calls = {"n": 0}

    def busy():
        calls["n"] += 1
        return calls["n"] < 3  # 前两次 True（回复处理中），第三次起 False

    sched.set_reply_busy(busy)
    sleep = FakeSleep(stop, stop_after=4)
    sched._sleep = sleep
    asyncio.run(sched.run(stop))

    assert [t.text for t in queue.tasks] == ["甲"]
    assert sleep.delays[0] == 7 * 60          # 正常间隔等待
    assert sleep.delays[1:3] == [0.5, 0.5]    # 回复处理中的让位心跳
    assert calls["n"] == 3


class HangSleep:
    """模拟真实长睡：挂起直到任务被取消（记录每次调用时长）。"""

    def __init__(self):
        self.delays = []

    async def __call__(self, delay):
        self.delays.append(delay)
        await asyncio.Event().wait()  # 永不就绪；只有 cancel 能打断


async def _wait_delays(sleep, expect_len, timeout=2.0):
    t0 = time.monotonic()
    while len(sleep.delays) < expect_len:
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"等待 sleep 调用超时：{sleep.delays}")
        await asyncio.sleep(0.01)


def test_configure_wakes_long_sleep():
    """长睡中 configure 改节奏 → 立即打断重算，不等旧周期睡完。

    线上事故（2026-09-13 日志复盘）：启动时默认存档 interval=420s 进入长睡，
    随后切存档（10s）configure 只改数值不唤醒，调度器把旧觉睡满 458s 才产出
    第一条话题——「开始直播后 6 分钟不讲话题」。
    """
    queue = FakeQueue()
    sleep = HangSleep()
    sched = Scheduler(queue, config=make_config(["甲"]),
                      random=FakeRandom(0.0), sleep=sleep)
    sched.set_live(True)

    async def scenario():
        run_task = asyncio.create_task(sched.run(asyncio.Event()))
        # c1 被 set_live 预置的 wake 立即打断（woken 空转重算），c2 才挂起长睡
        await _wait_delays(sleep, 2)
        assert sleep.delays == [420.0, 420.0]
        sched.configure(interval_s=1.0, jitter_s=0.0)   # 新节奏应立即唤醒重算
        await _wait_delays(sleep, 3)
        assert sleep.delays == [420.0, 420.0, 1.0]
        run_task.cancel()
        await run_task

    asyncio.run(scenario())
    assert queue.tasks == []  # 未到任何发射点，只验证唤醒与节奏重算


def test_end_live_during_long_sleep_no_emit():
    """长睡中被「结束直播」：立即唤醒回空转、不产出话题。

    线上事故同上：结束直播后，睡满旧周期的调度器仍播了一条（睡醒未复查
    门控）——现在 set_live 打断长睡 + emit 前复查双保险。
    """
    queue = FakeQueue()
    sleep = HangSleep()
    sched = Scheduler(queue, config=make_config(["甲"]),
                      random=FakeRandom(0.0), sleep=sleep)
    sched.set_live(True)

    async def scenario():
        run_task = asyncio.create_task(sched.run(asyncio.Event()))
        # c1 被 set_live 预置的 wake 立即打断（woken 空转重算），c2 才挂起长睡
        await _wait_delays(sleep, 2)
        assert sleep.delays == [420.0, 420.0]
        sched.set_live(False)              # 睡觉期间结束直播
        await _wait_delays(sleep, 3)
        assert sleep.delays[2] == 1.0      # 回 1s 空转心跳而非继续发射流程
        run_task.cancel()
        await run_task

    asyncio.run(scenario())
    assert queue.tasks == []


def test_end_live_before_emit_no_topic():
    """睡满到期但 live 已 False：emit 前门控复查拦截（FakeSleep 同步切 live）。"""

    class HookSleep(FakeSleep):
        async def __call__(self, delay):
            await super().__call__(delay)
            if self.count == 1:
                sched.set_live(False)      # 睡满瞬间结束直播

    stop = asyncio.Event()
    queue = FakeQueue()
    sched = Scheduler(queue, config=make_config(["甲"]), random=FakeRandom(0.0))
    sleep = HookSleep(stop=stop, stop_after=3)
    sched._sleep = sleep
    sched.set_live(True)
    asyncio.run(sched.run(stop))

    assert queue.tasks == []
    assert sleep.delays == [7 * 60, 1.0, 1.0]  # 到期 → 复查拦截 → 空转心跳


def test_end_live_during_reply_busy_no_emit():
    """让位等待中被「结束直播」：回复空闲后本轮不播（让位循环门控复查）。"""
    stop = asyncio.Event()
    queue = FakeQueue()
    sched = Scheduler(queue, config=make_config(["甲"]), random=FakeRandom(0.0))
    sched.set_live(True)
    calls = {"n": 0}

    def busy():
        calls["n"] += 1
        if calls["n"] >= 2:
            sched.set_live(False)          # 让位期间结束直播
            return False                   # 回复同时结束
        return True

    sched.set_reply_busy(busy)
    sleep = FakeSleep(stop, stop_after=3)
    sched._sleep = sleep
    asyncio.run(sched.run(stop))

    assert queue.tasks == []
    assert sleep.delays == [7 * 60, 0.5, 1.0]  # 长睡 → 让位心跳一次 → 空转
