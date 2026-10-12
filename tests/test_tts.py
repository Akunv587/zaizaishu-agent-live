"""流式 TTS 单元测试（PROMPTS 步骤 4.2）。

不用 mock httpx：用标准库 http.server 起本地服务返回伪造 SSE 行序列，
把 MiniMaxVoiceClient 的 endpoint 指向它。覆盖正常多分块、错误码、
坏行跳过、请求体形态；再覆盖 hub.tts 异步适配器的块传递、错误重抛、
连续失败通知与慢消费者下的完整性（背压不丢块）。
"""
import asyncio
import json
import sys
import threading
import wave
from binascii import hexlify
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from hub.stats import StatsCollector
from hub.tts import TTSError, TTSClient, save_wav

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "voice_clone"))
from minimax_client import MiniMaxError, MiniMaxVoiceClient  # noqa: E402

TTS_CFG = {"tts": {"voice_id": "v-test", "model": "speech-2.8-hd",
                   "speed": 1.0, "vol": 1.0, "pitch": 0}}


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


def audio_line(data: bytes) -> bytes:
    return b'data:{"data":{"audio":"' + hexlify(data) + b'"}}'


def audio_line_status(data: bytes, status: int) -> bytes:
    """带 status 字段的 SSE 音频行（真实协议：1=增量分块，2=收尾事件）。"""
    return (b'data:{"data":{"audio":"' + hexlify(data) + b'","status":'
            + str(status).encode() + b'}}')


ERROR_LINE = (b'data:{"base_resp":{"status_code":1004,"status_msg":"invalid api key"},'
              b'"trace_id":"t-1"}')


class SSEHandler(BaseHTTPRequestHandler):
    lines: list = []
    tail: bytes = b""
    captured: dict = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        type(self).captured = json.loads(self.rfile.read(length).decode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for line in type(self).lines:
            self.wfile.write(line + b"\n")
            self.wfile.flush()
        if type(self).tail:
            self.wfile.write(type(self).tail)
            self.wfile.flush()

    def log_message(self, *args):
        pass


@pytest.fixture()
def sse_server():
    holder = {}

    def start(lines, tail=b""):
        class Handler(SSEHandler):
            pass
        Handler.lines = list(lines)
        Handler.tail = tail
        Handler.captured = {}
        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        holder["server"] = server
        return f"http://127.0.0.1:{port}", Handler

    yield start
    srv = holder.get("server")
    if srv:
        srv.shutdown()
        srv.server_close()


class FakeStreamClient:
    """同步生成器假客户端（不联网）：chunks 逐块产出后可选抛 error。"""

    def __init__(self, chunks=None, error=None):
        self._chunks = list(chunks or [])
        self._error = error
        self.calls = []

    def synthesize_stream(self, text, voice_id, model=None, speed=1, vol=1,
                          pitch=0, emotion=None):
        self.calls.append({"text": text, "voice_id": voice_id, "model": model,
                           "speed": speed, "vol": vol, "pitch": pitch,
                           "emotion": emotion})
        for c in self._chunks:
            yield c
        if self._error is not None:
            raise self._error


# ---------- 第一层：MiniMaxVoiceClient.synthesize_stream ----------

def test_client_stream_yields_chunks(sse_server):
    url, _ = sse_server([
        audio_line(b"chunk-1"),
        audio_line(b"chunk-2"),
        b'data:{"extra_info":{"usage_characters":5}}',   # 无 audio 的行：跳过
        audio_line(b"chunk-3"),
        b'data:{"data":{"audio":""},"is_final":true}',  # 空 audio：跳过
    ])
    client = MiniMaxVoiceClient("test-key", endpoint=url)
    chunks = list(client.synthesize_stream("你好兄弟们", voice_id="demoVoice001"))
    assert chunks == [b"chunk-1", b"chunk-2", b"chunk-3"]


def test_client_stream_request_body(sse_server):
    url, handler = sse_server([audio_line(b"ab")])
    client = MiniMaxVoiceClient("test-key", endpoint=url, model="speech-2.8-hd")
    list(client.synthesize_stream("hi", voice_id="v1", speed=1.5, vol=2, pitch=1))
    body = handler.captured
    assert body["model"] == "speech-2.8-hd"
    assert body["text"] == "hi"
    assert body["stream"] is True
    assert body["output_format"] == "hex"
    assert body["voice_setting"]["voice_id"] == "v1"
    assert body["voice_setting"]["speed"] == 1.5  # 浮点保留两位
    assert body["voice_setting"]["vol"] == 2.0
    assert body["voice_setting"]["pitch"] == 1
    assert body["audio_setting"]["sample_rate"] == 24000
    assert body["audio_setting"]["bitrate"] == 128000
    assert body["audio_setting"]["format"] == "pcm"
    assert body["audio_setting"]["channel"] == 1


def test_client_stream_emotion_and_pause_marks(sse_server):
    """emotion 透传 + 「|」自动转官方 <#秒数#> 停顿标记（每竖线 0.4s）。"""
    url, handler = sse_server([audio_line(b"ab")])
    client = MiniMaxVoiceClient("test-key", endpoint=url)
    list(client.synthesize_stream("家人们好||欢迎", voice_id="v1", emotion="happy"))
    body = handler.captured
    assert body["voice_setting"]["emotion"] == "happy"
    assert body["text"] == "家人们好<#0.8#>欢迎"


def test_client_stream_pause_marks_native_passthrough(sse_server):
    """文本已含官方 <#…#> 标记时原样放行不二次转换。"""
    url, handler = sse_server([audio_line(b"ab")])
    client = MiniMaxVoiceClient("test-key", endpoint=url)
    list(client.synthesize_stream("你好<#1.5#>世界|", voice_id="v1"))
    body = handler.captured
    assert body["text"] == "你好<#1.5#>世界|"   # 含 <# 时竖线不再转换


def test_client_stream_float_speed_vol_rounding(sse_server):
    """speed/vol 接受浮点，分别保留 2/1 位小数；pitch 取整。"""
    url, handler = sse_server([audio_line(b"ab")])
    client = MiniMaxVoiceClient("test-key", endpoint=url)
    list(client.synthesize_stream("hi", voice_id="v1",
                                  speed=1.234, vol=2.567, pitch=1.6))
    body = handler.captured
    assert body["voice_setting"]["speed"] == 1.23
    assert body["voice_setting"]["vol"] == 2.6
    assert body["voice_setting"]["pitch"] == 2


def test_client_delete_voice_rejects_bad_type():
    """delete_voice 仅接受 voice_cloning / voice_generation（系统音色不可删）。"""
    client = MiniMaxVoiceClient("test-key", endpoint="http://127.0.0.1:1")
    with pytest.raises(ValueError):
        client.delete_voice("v1", "system")
    with pytest.raises(ValueError):
        client.delete_voice("v1", "all")


def test_client_stream_error_raises(sse_server):
    url, _ = sse_server([audio_line(b"first"), ERROR_LINE])
    client = MiniMaxVoiceClient("test-key", endpoint=url)
    with pytest.raises(MiniMaxError) as exc:
        list(client.synthesize_stream("x", voice_id="v"))
    assert exc.value.status_code == 1004
    assert "invalid api key" in exc.value.status_msg


def test_client_stream_skips_bad_lines(sse_server):
    url, _ = sse_server([
        b": keep-alive comment",   # 非 data 行
        b"data:not-json{{{",       # 坏 JSON 行：跳过不中断
        audio_line(b"ok-1"),
        b"data: ",                 # 空 body
        audio_line(b"ok-2"),
    ])
    client = MiniMaxVoiceClient("test-key", endpoint=url)
    assert list(client.synthesize_stream("x", voice_id="v")) == [b"ok-1", b"ok-2"]


def test_client_stream_tail_without_newline(sse_server):
    url, _ = sse_server([audio_line(b"a")], tail=audio_line(b"tail"))
    client = MiniMaxVoiceClient("test-key", endpoint=url)
    assert list(client.synthesize_stream("x", voice_id="v")) == [b"a", b"tail"]


def test_client_stream_drops_final_full_audio(sse_server):
    """回归（2026-09-14「说两遍」根因）：status=2 收尾事件携带整条全量音频，
    已收到增量分块时必须丢弃，否则正文被播放两遍。"""
    c1, c2 = b"chunk-1", b"chunk-2"
    url, _ = sse_server([
        audio_line_status(c1, 1),
        audio_line_status(c2, 1),
        audio_line_status(c1 + c2, 2),   # 全量重发：应被丢弃
    ])
    client = MiniMaxVoiceClient("test-key", endpoint=url)
    assert list(client.synthesize_stream("x", voice_id="v")) == [c1, c2]


def test_client_stream_final_only_audio_kept(sse_server):
    """全流仅 status=2 收尾事件带音频（极短文本无增量分块）时用它保底。"""
    url, _ = sse_server([audio_line_status(b"only", 2)])
    client = MiniMaxVoiceClient("test-key", endpoint=url)
    assert list(client.synthesize_stream("x", voice_id="v")) == [b"only"]


def test_client_stream_final_tail_chunk_kept(sse_server):
    """长度护栏：status=2 音频短于增量总和（协议变体末块）时照常产出防截尾。"""
    c1, c2 = b"chunk-1", b"chunk-2"
    url, _ = sse_server([
        audio_line_status(c1, 1),
        audio_line_status(c2, 1),
        audio_line_status(b"tail", 2),   # 短尾块：不是全量重发，应保留
    ])
    client = MiniMaxVoiceClient("test-key", endpoint=url)
    assert list(client.synthesize_stream("x", voice_id="v")) == [c1, c2, b"tail"]


def test_client_stream_final_without_audio_skipped(sse_server):
    """status=2 收尾事件无音频（仅 extra_info 计费信息）正常跳过。"""
    url, _ = sse_server([
        audio_line_status(b"ab", 1),
        b'data:{"data":{"status":2},"extra_info":{"usage_characters":2}}',
    ])
    client = MiniMaxVoiceClient("test-key", endpoint=url)
    assert list(client.synthesize_stream("x", voice_id="v")) == [b"ab"]


# ---------- 第二层：hub.tts 异步适配器 ----------

def test_async_adapter_chunks_via_sse(sse_server):
    url, _ = sse_server([audio_line(b"c1"), audio_line(b"c2"),
                         b'data:{"extra_info":{}}'])
    stats = StatsCollector()
    tts = TTSClient(config=TTS_CFG, stats=stats,
                    client=MiniMaxVoiceClient("k", endpoint=url))

    async def collect():
        return [c async for c in tts.synthesize_stream("hi")]

    assert asyncio.run(collect()) == [b"c1", b"c2"]
    assert stats.get("tts_fail") == 0


def test_async_adapter_propagates_error_and_counts(sse_server):
    url, _ = sse_server([audio_line(b"c1"), ERROR_LINE])
    stats = StatsCollector()
    tts = TTSClient(config=TTS_CFG, stats=stats,
                    client=MiniMaxVoiceClient("k", endpoint=url))

    async def run():
        got = []
        try:
            async for c in tts.synthesize_stream("hi"):
                got.append(c)
        except MiniMaxError as e:
            return got, e

    got, err = asyncio.run(run())
    assert got == [b"c1"]
    assert err.status_code == 1004
    assert stats.get("tts_fail") == 1


def test_adapter_keeps_all_chunks_with_slow_consumer():
    # 100 块远超队列容量 32：慢消费下 worker 必须被背压减速，块不丢、顺序不乱
    chunks = [bytes([i % 251]) * 64 for i in range(100)]
    tts = TTSClient(config=TTS_CFG, client=FakeStreamClient(chunks=chunks))

    async def collect():
        out = []
        async for c in tts.synthesize_stream("x"):
            out.append(c)
            await asyncio.sleep(0.001)
        return out

    assert asyncio.run(collect()) == chunks


def test_consecutive_failures_notify_at_threshold():
    stats = StatsCollector()
    notified = []
    tts = TTSClient(config=TTS_CFG, stats=stats,
                    client=FakeStreamClient(error=MiniMaxError(1004, "bad key")),
                    notifier=lambda t, m: notified.append((t, m)))

    async def fail_once():
        try:
            async for _ in tts.synthesize_stream("x"):
                pass
        except MiniMaxError:
            pass

    for _ in range(3):
        asyncio.run(fail_once())
    assert len(notified) == 1            # 第 3 次触发通知
    assert stats.get("tts_fail") == 3
    asyncio.run(fail_once())             # 第 4 次：计数已重置，不再通知
    assert len(notified) == 1
    assert stats.get("tts_fail") == 4


def test_success_resets_failure_streak():
    notified = []
    tts = TTSClient(config=TTS_CFG,
                    client=FakeStreamClient(error=MiniMaxError(1004, "x")),
                    notifier=lambda t, m: notified.append((t, m)))

    async def fail_once():
        try:
            async for _ in tts.synthesize_stream("x"):
                pass
        except MiniMaxError:
            pass

    async def succeed_once():
        async for _ in tts.synthesize_stream("x"):
            pass

    for _ in range(2):
        asyncio.run(fail_once())
    assert notified == []
    tts._client = FakeStreamClient(chunks=[b"ab"])
    asyncio.run(succeed_once())          # 成功清零连续计数
    tts._client = FakeStreamClient(error=MiniMaxError(1004, "x"))
    for _ in range(2):
        asyncio.run(fail_once())
    assert notified == []                # 2 次失败未达阈值
    asyncio.run(fail_once())
    assert len(notified) == 1


def test_missing_key_and_voice_id():
    with pytest.raises(TTSError):
        TTSClient(config={"tts": {}})  # 缺 voice_id


def test_save_wav(tmp_path):
    pcm = b"\x01\x00\x02\x00" * 50  # 200 字节 = 100 帧 int16
    path = tmp_path / "out.wav"
    returned = save_wav(pcm, path)
    with wave.open(returned, "rb") as w:
        assert w.getframerate() == 24000
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getnframes() == 100
