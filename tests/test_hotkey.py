"""F9 热键暂停语义的单元测试（PROMPTS 步骤 4.4）。

验收口径（PRD FR-5）：暂停立即生效——Router 停止产出新任务（事件照收只记录）、
队列清空含 P0、Player 挂起；恢复后从新弹幕重新应答。
热键监听本身不测（全局键盘钩子不适合自动化），程序化触发 toggle()/on_hotkey()。
"""
import asyncio
import threading
import time

import pytest

from hub.events import LiveEvent, SpeakTask
from hub.hotkey import HotkeyController
from hub.queue import PrioritySpeakQueue
from hub.router import Router


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FixedRandom:
    def random(self):
        return 0.5


class FakePlayer:
    def __init__(self):
        self.calls = []
        self._playing = None

    @property
    def is_playing(self):
        return self._playing is not None

    async def pause(self):
        self.calls.append("pause")

    async def resume(self):
        self.calls.append("resume")


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


def echo_builder(ctx):
    """同步假生成器：热键测试只关心暂停/恢复语义，需有生成器才能产出 chat 任务。"""
    return f"回复：{ctx['content']}"


def make_router(tasks, clock):
    cfg = {
        "events": {"chat": {"enabled": True, "merge_window_s": 3, "ignore_noise_p": 0.2}},
        "safety": {"fallback_reply": "兜底"},
    }
    return Router(tasks.append, config=cfg, clock=clock, random=FixedRandom(),
                  reply_builder=echo_builder)


def chat(user, content, ts):
    return LiveEvent(type="chat", user=user, content=content, ts=ts)


def make_controller(tasks, clock, messages, loop=None):
    router = make_router(tasks, clock)
    queue = PrioritySpeakQueue(config={"events": {"chat": {"stale_drop_s": 30}}})
    player = FakePlayer()
    ctrl = HotkeyController(router, queue, player,
                            config={"safety": {"pause_hotkey": "f9"}},
                            notify=messages.append, loop=loop)
    return ctrl, router, queue, player


def _drain(router):
    asyncio.run(router.drain(timeout=2))


def test_pause_suppresses_router_task_production():
    """暂停期间弹幕到达：只记录不产出；恢复后新弹幕正常产出任务。"""
    tasks, clock, messages = [], FakeClock(0.0), []

    async def scenario():
        ctrl, router, _, _ = make_controller(tasks, clock, messages)
        await ctrl.toggle()  # 暂停
        assert ctrl.paused is True

        router.handle_event(chat("甲", "主播这是什么游戏", 0))
        clock.advance(3.5)
        router.scan()
        await router.drain(timeout=2)
        assert tasks == []  # 暂停期间无任务产出

        await ctrl.toggle()  # 恢复
        assert ctrl.paused is False
        router.handle_event(chat("乙", "恢复后的新弹幕", 4))
        clock.advance(3.5)
        router.scan()
        await router.drain(timeout=2)
        assert len(tasks) == 1
        assert tasks[0].source == "chat"

    asyncio.run(scenario())
    assert any("[已暂停发声]" in m for m in messages)
    assert any("[已恢复" in m for m in messages)


def test_pause_records_event_without_task_and_logs(tmp_path):
    """暂停期间事件被记录（paused_event_recorded 日志留痕）而非静默丢弃。"""
    from datetime import datetime
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "info")
    tasks, clock, messages = [], FakeClock(0.0), []

    async def scenario():
        ctrl, router, _, _ = make_controller(tasks, clock, messages)
        await ctrl.toggle()
        router.handle_event(chat("甲", "暂停期间的弹幕", 0))
        clock.advance(3.5)
        router.scan()
        await router.drain(timeout=2)
        assert tasks == []

    asyncio.run(scenario())
    day = datetime.now().strftime("%Y%m%d")
    content = (tmp_path / f"live-{day}.log").read_text(encoding="utf-8")
    assert "paused_event_recorded" in content


def test_pause_clears_queue_backlog_including_p0():
    """暂停清空全部积压（含 P0 礼物层），get() 挂起。"""
    tasks, clock, messages = [], FakeClock(0.0), []

    async def scenario():
        ctrl, _, queue, player = make_controller(tasks, clock, messages)
        await queue.put(SpeakTask(priority=0, text="礼物感谢", event_ts=time.time(), source="gift"))
        await queue.put(SpeakTask(priority=1, text="回复", event_ts=time.time(), source="chat"))
        assert queue.qstat()["total"] == 2

        await ctrl.toggle()  # 暂停
        assert queue.qstat()["total"] == 0
        assert queue.paused is True
        assert player.calls == ["pause"]

        # 暂停期间 get() 挂起（不会立即拿到任务）
        getter = asyncio.ensure_future(queue.get())
        await asyncio.sleep(0.05)
        assert not getter.done()
        getter.cancel()

        await ctrl.toggle()  # 恢复
        assert queue.paused is False
        assert player.calls == ["pause", "resume"]

    asyncio.run(scenario())


def test_on_hotkey_from_listener_thread_flips_state():
    """pynput 监听线程路径：on_hotkey 经 call_soon_threadsafe 安全投递并生效。"""
    tasks, clock, messages = [], FakeClock(0.0), []

    async def scenario():
        loop = asyncio.get_running_loop()
        ctrl, _, _, _ = make_controller(tasks, clock, messages, loop=loop)

        thread = threading.Thread(target=lambda: (time.sleep(0.02), ctrl.on_hotkey()))
        thread.start()
        for _ in range(200):
            if ctrl.paused:
                break
            await asyncio.sleep(0.01)
        thread.join()
        assert ctrl.paused is True

        ctrl.on_hotkey()  # 恢复（同一线程安全路径）
        for _ in range(200):
            if not ctrl.paused:
                break
            await asyncio.sleep(0.01)
        assert ctrl.paused is False

    asyncio.run(scenario())


def test_toggle_is_idempotent_and_ordered():
    """连按去抖：同一状态重复 toggle 请求不产生多余动作；暂停→恢复按序生效。"""
    tasks, clock, messages = [], FakeClock(0.0), []

    async def scenario():
        ctrl, _, _, player = make_controller(tasks, clock, messages)
        await asyncio.gather(ctrl.toggle(), ctrl.toggle(), ctrl.toggle())  # 请求序列：暂停/恢复/暂停
        await asyncio.sleep(0.05)
        assert ctrl.paused is True  # 奇数次按最终暂停
        assert player.calls.count("pause") >= 1

    asyncio.run(scenario())
