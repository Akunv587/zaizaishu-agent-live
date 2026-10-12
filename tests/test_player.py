"""播放调度器的单元测试（PROMPTS 步骤 4.3）。

不碰真实声卡：FakeOutStream 注入 out_factory、FakeTTS 注入 stream 产块。
覆盖串行播放、首包开流、条间停顿、played 计数与端到端延迟采样、
单条失败跳过继续、暂停/恢复状态机、设备名校验与空闲安静挂起。
"""
import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

from hub.events import SpeakTask
from hub.player import Player, PlayerError
from hub.queue import PrioritySpeakQueue
from hub.stats import StatsCollector

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "voice_clone"))
from minimax_client import MiniMaxError  # noqa: E402


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


class FakeOutStream:
    instances = []

    def __init__(self):
        self.written = bytearray()
        self.opened_at = time.monotonic()
        self.started = False
        self.stopped = False
        self.closed = False
        type(self).instances.append(self)

    def start(self):
        self.started = True

    def write(self, data):
        self.written.extend(data)

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class FakeTTS:
    """按请求顺序产块；可让第 N 次请求抛错。"""

    def __init__(self, chunks=3, chunk_size=64, fail_on_request=None):
        self._chunks = chunks
        self._chunk_size = chunk_size
        self._fail_on = fail_on_request
        self.requests = []

    async def synthesize_stream(self, text):
        self.requests.append(text)
        if self._fail_on is not None and len(self.requests) == self._fail_on:
            raise MiniMaxError(1004, "合成失败")
        for _ in range(self._chunks):
            yield b"\x11" * self._chunk_size


def make_task(text="测试话术", priority=1, source="chat", age_s=0.0):
    return SpeakTask(priority=priority, text=text, event_ts=time.time() - age_s,
                     source=source)


_UNSET = object()


class EmotionTTS(FakeTTS):
    """支持逐条 emotion/speed 覆盖的假 TTS；记录覆盖入参（未传记为 _UNSET）。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.overrides = []

    async def synthesize_stream(self, text, emotion=_UNSET, speed=_UNSET, **kwargs):
        self.overrides.append((text, emotion, speed))
        self.requests.append(text)
        for _ in range(self._chunks):
            yield b"\x22" * self._chunk_size


def make_player(queue, stats, tts, gap_ms=60, device=""):
    cfg = {"audio": {"device": device, "sample_rate": 24000,
                     "channels": 1, "gap_ms": gap_ms}}
    FakeOutStream.instances = []
    player = Player(queue, tts, stats=stats, config=cfg,
                    out_factory=FakeOutStream)
    return player, FakeOutStream.instances


async def wait_until(cond, timeout=3.0):
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.01)


def make_wav(path: Path, rate=24000, frames=4800) -> str:
    import wave
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x01\x00" * frames)
    return str(path)


def test_audio_file_played_without_tts(tmp_path):
    """v3.1 预录音频任务：直接播文件，不请求 TTS，played 计数照常。"""
    wav = make_wav(tmp_path / "topic.wav")
    stats = StatsCollector()
    queue = PrioritySpeakQueue(config={})
    tts = FakeTTS(chunks=2)
    player, streams = make_player(queue, stats, tts)
    task = make_task("话题文本")
    task.audio_path = wav

    async def scenario():
        stop = asyncio.Event()
        run_task = asyncio.create_task(player.run(stop))
        await queue.put(task)
        await wait_until(lambda: stats.get("played") == 1)
        stop.set()
        await run_task

    asyncio.run(scenario())
    assert tts.requests == []                      # 未走 TTS
    assert len(streams) == 1
    assert bytes(streams[0].written) == b"\x01\x00" * 4800  # 完整 PCM 写出


def test_teardown_close_errors_not_warnings():
    """暂停/关停撞停在飞 write 产生的 paStreamIsStopped(-9983) 属关停常态：
    关流错误不得记 warning（降级 debug 留痕）、不得上抛打断主循环。"""

    class RecLog:
        def __init__(self):
            self.debugs, self.infos, self.warnings = [], [], []

        def debug(self, event, extra=None):
            self.debugs.append(event)

        def info(self, event, extra=None):
            self.infos.append(event)

        def warning(self, event, extra=None):
            self.warnings.append(event)

    class FragileOutStream:
        """stop/close 均抛 -9983 形态错误（模拟取消时在飞 write 与关流竞态）。"""

        def __init__(self):
            self.written = bytearray()

        def start(self):
            pass

        def write(self, data):
            self.written.extend(data)

        def stop(self):
            raise RuntimeError("Error stopping stream: Stream is stopped [PaErrorCode -9983]")

        def close(self):
            raise RuntimeError("Error closing stream: Stream is stopped [PaErrorCode -9983]")

    async def scenario():
        rec = RecLog()
        stats = StatsCollector()
        queue = PrioritySpeakQueue(config={})
        cfg = {"audio": {"device": "", "sample_rate": 24000,
                         "channels": 1, "gap_ms": 0}}
        player = Player(queue, FakeTTS(chunks=1), stats=stats, config=cfg,
                        out_factory=FragileOutStream, log=rec)
        stop = asyncio.Event()
        run_task = asyncio.create_task(player.run(stop))
        await queue.put(make_task("关停演示"))
        await wait_until(lambda: stats.get("played") == 1)
        stop.set()
        await run_task
        return rec

    rec = asyncio.run(scenario())
    assert rec.warnings == []                       # 关停路径零 warning
    assert "stream_stop_skipped" in rec.debugs      # 降级 debug 留痕
    assert "stream_close_skipped" in rec.debugs


def test_stream_write_offloaded_keeps_event_loop_responsive(tmp_path):
    """stream.write 同步阻塞（预制音频连写可冻循环数十秒）→ 必须 to_thread 卸载：
    写线程阻塞期间，事件循环上其他协程照常调度、播放最终完成。"""
    gate = threading.Event()

    class BlockingOutStream:
        def __init__(self):
            self.written = bytearray()

        def start(self):
            pass

        def write(self, data):
            # 若 write 在事件循环线程同步执行，gate 协程将永远没机会 set
            if not gate.wait(timeout=2):
                raise RuntimeError("事件循环被同步 write 冻住")
            self.written.extend(data)

        def stop(self):
            pass

        def close(self):
            pass

    wav = make_wav(tmp_path / "gated.wav")
    stats = StatsCollector()
    queue = PrioritySpeakQueue(config={})
    cfg = {"audio": {"device": "", "sample_rate": 24000,
                     "channels": 1, "gap_ms": 0}}
    player = Player(queue, FakeTTS(), stats=stats, config=cfg,
                    out_factory=BlockingOutStream)
    task = make_task("阻塞写测试")
    task.audio_path = wav

    async def scenario():
        stop = asyncio.Event()
        run_task = asyncio.create_task(player.run(stop))
        await queue.put(task)

        async def opener():
            for _ in range(10):
                await asyncio.sleep(0.02)   # 写阻塞期间循环必须仍在调度
            gate.set()

        await asyncio.wait_for(opener(), timeout=3)
        await wait_until(lambda: stats.get("played") == 1)
        stop.set()
        await run_task

    asyncio.run(scenario())
    assert stats.get("played") == 1


def test_audio_file_bad_format_falls_back_to_tts(tmp_path):
    """异参 wav 解码失败：回退 TTS 念文本，audio_file_fail 计数。"""
    wav = make_wav(tmp_path / "odd.wav", rate=16000)
    stats = StatsCollector()
    queue = PrioritySpeakQueue(config={})
    tts = FakeTTS(chunks=2)
    player, streams = make_player(queue, stats, tts)
    task = make_task("兜底文本")
    task.audio_path = wav

    async def scenario():
        stop = asyncio.Event()
        run_task = asyncio.create_task(player.run(stop))
        await queue.put(task)
        await wait_until(lambda: stats.get("played") == 1)
        stop.set()
        await run_task

    asyncio.run(scenario())
    assert tts.requests == ["兜底文本"]
    assert stats.get("audio_file_fail") == 1
    assert stats.get("played") == 1


def test_audio_file_missing_and_no_text_drops_silently(tmp_path):
    """文件丢失且无文本可念：本条放弃，不炸主循环。"""
    stats = StatsCollector()
    queue = PrioritySpeakQueue(config={})
    tts = FakeTTS(chunks=1)
    player, streams = make_player(queue, stats, tts)
    task = make_task("")
    task.audio_path = str(tmp_path / "不存在.wav")

    async def scenario():
        stop = asyncio.Event()
        run_task = asyncio.create_task(player.run(stop))
        await queue.put(task)
        await wait_until(lambda: stats.get("audio_file_fail") == 1)
        stop.set()
        await run_task

    asyncio.run(scenario())
    assert tts.requests == []
    assert stats.get("played") == 0
    assert streams == []


def test_plays_tasks_serially_with_gap():
    stats = StatsCollector()
    queue = PrioritySpeakQueue(config={"events": {"chat": {"stale_drop_s": 30}}})
    tts = FakeTTS(chunks=3)
    player, streams = make_player(queue, stats, tts, gap_ms=80)

    async def scenario():
        stop = asyncio.Event()
        run_task = asyncio.create_task(player.run(stop))
        await queue.put(make_task("第一条话术"))
        await queue.put(make_task("第二条话术"))
        await wait_until(lambda: stats.get("played") == 2)
        stop.set()
        await run_task

    asyncio.run(scenario())

    assert [s for s in tts.requests] == ["第一条话术", "第二条话术"]
    assert len(streams) == 2                        # 一条一个流，无重叠
    assert all(s.started for s in streams)          # 开流即 start（PortAudio 回归）
    assert all(s.closed for s in streams)
    assert all(len(s.written) == 3 * 64 for s in streams)
    gap = streams[1].opened_at - streams[0].opened_at
    assert gap >= 0.08 - 0.02                       # 条间停顿生效（含容差）


def test_played_counted_and_latency_recorded():
    stats = StatsCollector()
    queue = PrioritySpeakQueue(config={"events": {"chat": {}}})
    tts = FakeTTS(chunks=2)
    player, _ = make_player(queue, stats, tts, gap_ms=0)

    async def scenario():
        stop = asyncio.Event()
        run_task = asyncio.create_task(player.run(stop))
        await queue.put(make_task("带年龄的话术", age_s=0.5))
        await wait_until(lambda: stats.get("played") == 1)
        stop.set()
        await run_task

    asyncio.run(scenario())
    assert stats.get("played") == 1
    assert stats.summary()["latency_samples"] == 1
    assert 0.4 <= stats.summary()["latency_avg_s"] <= 5.0  # 含 event_ts 年龄


def test_tts_error_skips_task_and_continues():
    stats = StatsCollector()
    queue = PrioritySpeakQueue(config={"events": {"chat": {}}})
    tts = FakeTTS(chunks=2, fail_on_request=1)
    player, streams = make_player(queue, stats, tts, gap_ms=0)

    async def scenario():
        stop = asyncio.Event()
        run_task = asyncio.create_task(player.run(stop))
        await queue.put(make_task("会失败的话术"))
        await queue.put(make_task("正常的话术"))
        await wait_until(lambda: stats.get("played") == 1)
        await asyncio.sleep(0.05)
        stop.set()
        await run_task

    asyncio.run(scenario())
    assert stats.get("played") == 1           # 失败条不计 played
    assert len(streams) == 1                  # 仅成功条开流（失败发生在首块前）
    assert len(streams[0].written) == 2 * 64
    assert tts.requests == ["会失败的话术", "正常的话术"]  # 主循环未中断


def test_emotion_onsite_passes_override_and_counts():
    """v4：未预生成情绪 → 现场 TTS 带该情绪/语速，并记 emotion_onsite。"""
    stats = StatsCollector()
    queue = PrioritySpeakQueue(config={})
    tts = EmotionTTS(chunks=2)
    player, streams = make_player(queue, stats, tts)
    task = make_task("未预生成的语气")
    task.emotion = "happy"
    task.speed = 1.1

    async def scenario():
        stop = asyncio.Event()
        run_task = asyncio.create_task(player.run(stop))
        await queue.put(task)
        await wait_until(lambda: stats.get("played") == 1)
        stop.set()
        await run_task

    asyncio.run(scenario())
    assert tts.overrides == [("未预生成的语气", "happy", 1.1)]
    assert stats.get("emotion_onsite") == 1
    assert len(streams) == 1


def test_emotion_default_uses_configured_params():
    """v4：default 是内部兜底档名、不是 MiniMax 情绪取值——按「无覆盖」处理。"""
    stats = StatsCollector()
    queue = PrioritySpeakQueue(config={})
    tts = EmotionTTS(chunks=1)
    player, streams = make_player(queue, stats, tts)
    task = make_task("兜底档")
    task.emotion = "default"

    async def scenario():
        stop = asyncio.Event()
        run_task = asyncio.create_task(player.run(stop))
        await queue.put(task)
        await wait_until(lambda: stats.get("played") == 1)
        stop.set()
        await run_task

    asyncio.run(scenario())
    assert tts.overrides == [("兜底档", _UNSET, _UNSET)]   # 不带任何覆盖参数
    assert tts.requests == ["兜底档"]
    assert stats.get("emotion_onsite") in (None, 0)


def test_emotion_override_unsupported_tts_falls_back():
    """注入的 TTS 不支持逐条覆盖时不炸：退回无参合成，本条照常出声。"""
    stats = StatsCollector()
    queue = PrioritySpeakQueue(config={})
    tts = FakeTTS(chunks=1)                    # 只接受 text
    player, streams = make_player(queue, stats, tts)
    task = make_task("降级文本")
    task.emotion = "happy"

    async def scenario():
        stop = asyncio.Event()
        run_task = asyncio.create_task(player.run(stop))
        await queue.put(task)
        await wait_until(lambda: stats.get("played") == 1)
        stop.set()
        await run_task

    asyncio.run(scenario())
    assert tts.requests == ["降级文本"]
    assert stats.get("played") == 1
    assert len(streams) == 1


def test_pause_after_current_then_resume():
    stats = StatsCollector()
    queue = PrioritySpeakQueue(config={"events": {"chat": {}}})
    tts = FakeTTS(chunks=2)
    player, streams = make_player(queue, stats, tts, gap_ms=0)

    async def scenario():
        stop = asyncio.Event()
        run_task = asyncio.create_task(player.run(stop))
        await queue.put(make_task("第一条"))
        await wait_until(lambda: len(streams) == 1)
        await player.pause()          # 立即中断当前句（快流下通常已播完）
        await queue.pause()           # 热键语义：清空积压 + get 挂起
        await queue.put(make_task("暂停期间入队"))
        await asyncio.sleep(0.15)
        assert stats.get("played") == 1        # 第二条不被消费
        assert player.state == "paused"
        await player.resume()
        await queue.resume()
        await wait_until(lambda: stats.get("played") == 2)
        stop.set()
        await run_task

    asyncio.run(scenario())
    assert stats.get("played") == 2


def test_pause_interrupts_current_sentence():
    """立即暂停：正在播的这句被中断关流，不再等到播完；played 不计数。"""
    stats = StatsCollector()
    queue = PrioritySpeakQueue(config={"events": {"chat": {}}})
    tts = FakeTTS(chunks=1)
    player, streams = make_player(queue, stats, tts, gap_ms=0)

    async def slow_stream(text):       # 慢流：块间让出事件循环，让 pause 插在播放中途
        for _ in range(1000):
            yield b"\x11" * 64
            await asyncio.sleep(0.01)

    player._stream_factory = slow_stream

    async def scenario():
        stop = asyncio.Event()
        run_task = asyncio.create_task(player.run(stop))
        await queue.put(make_task("会被打断的这句"))
        await wait_until(lambda: len(streams) == 1 and len(streams[0].written) > 0)
        await player.pause()           # 立即中断（不是播完停）
        await asyncio.sleep(0.05)
        assert player.state == "paused"
        assert streams[0].stopped or streams[0].closed   # 流被关，声音立停
        assert stats.get("played") == 0                 # 未播完，不计 played
        await player.resume()           # 恢复后主循环方能退出
        stop.set()
        await run_task

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    assert stats.get("played") == 0


def test_check_device_missing_raises_with_list():
    queue = PrioritySpeakQueue(config={"events": {"chat": {}}})
    tts = FakeTTS()
    cfg = {"audio": {"device": "不存在的设备XX", "sample_rate": 24000,
                     "channels": 1, "gap_ms": 800}}
    player = Player(queue, tts, config=cfg)  # 无 out_factory：走真实声卡校验
    with pytest.raises(PlayerError) as exc:
        player.check_device()
    assert "不存在的设备XX" in str(exc.value)
    assert "可用输出设备" in str(exc.value)


def test_check_device_blank_uses_default_and_injected_skips():
    queue = PrioritySpeakQueue(config={"events": {"chat": {}}})
    tts = FakeTTS()
    player, _ = make_player(queue, StatsCollector(), tts)
    cfg_player = Player(queue, tts, config={"audio": {}},
                        out_factory=FakeOutStream)
    cfg_player.check_device()
    player.check_device()  # 注入路径不查设备：不抛


def test_run_waits_quietly_when_no_tasks():
    stats = StatsCollector()
    queue = PrioritySpeakQueue(config={"events": {"chat": {}}})
    player, _ = make_player(queue, stats, FakeTTS())

    async def scenario():
        stop = asyncio.Event()
        run_task = asyncio.create_task(player.run(stop))
        await asyncio.sleep(0.2)   # 无任务：挂起等 get 超时轮询
        assert not run_task.done()
        assert stats.get("played") == 0
        stop.set()
        await asyncio.wait_for(run_task, timeout=2)

    asyncio.run(scenario())
    assert player.state == "idle"


def test_device_fallback_hook_fires_on_missing_device(monkeypatch):
    """配置设备播放时找不到 → 回退系统默认输出 + 触发 on_device_fallback。

    静默回退曾让用户以为「切换设备没生效」（蓝牙耳机关机后名字解析不到），
    现在必须日志+回调双通道可见；设备找得到时不触发。
    """
    import types
    fake = types.ModuleType("sounddevice")
    fake.query_devices = lambda: [
        {"name": "CABLE Input (VB-Audio Virtual C", "max_output_channels": 2,
         "hostapi": 0}]
    fake.query_hostapis = lambda idx: {"name": "MME"}
    opened = []

    class FakeSdStream:
        def __init__(self, **kw):
            opened.append(kw.get("device"))
        def start(self): pass
        def write(self, data): pass
        def stop(self): pass
        def close(self): pass

    fake.RawOutputStream = FakeSdStream
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    queue = PrioritySpeakQueue(config={"events": {"chat": {}}})
    cfg = {"audio": {"device": "不存在的设备XX", "sample_rate": 24000,
                     "channels": 1, "gap_ms": 0}}
    player = Player(queue, FakeTTS(), config=cfg)  # 无 out_factory：走真实解析路径
    fallbacks = []
    player.on_device_fallback = fallbacks.append

    asyncio.run(player._play_task(make_task("测试")))
    assert fallbacks == ["不存在的设备XX"]
    assert opened == [None]  # 回退系统默认设备（device=None）

    player.swap_device("CABLE Input (VB-Audio Virtual C, MME")
    asyncio.run(player._play_task(make_task("测试2")))
    assert fallbacks == ["不存在的设备XX"]  # 设备找得到：不再触发
    assert opened[-1] == 0
