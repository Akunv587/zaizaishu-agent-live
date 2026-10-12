"""流式合成异步适配（PROMPTS 步骤 4.2，DESIGN §4.4/§7.3；PRD FR-1）。

两层设计的第二层：同步生成器在 voice_clone\\minimax_client.py（零依赖库），
本模块把它适配成 AsyncIterator[bytes] 供 asyncio 主循环消费：
  * 独立线程迭代同步生成器（urllib 阻塞 IO 不占事件循环）；
  * 分块经 asyncio.run_coroutine_threadsafe 投进有界 asyncio.Queue
    （maxsize 32）——队列满时工作线程阻塞在 put 上，天然背压；
  * 线程结束投哨兵收尾；MiniMaxError 经队列传回，async 侧重新抛出；
  * 连续失败 3 次 plyer 桌面通知并重置计数；每次失败对 stats 的
    tts_fail 计数（DESIGN §8：TTS 失败丢弃该条并计数，不重试）。

密钥不进 config.yaml：MiniMax Key 统一存 voice_clone\\voice_clone_data\\
config.json（向导维护，DESIGN §4.10 单一数据源）。
"""
import asyncio
import sys
import threading
import wave
from pathlib import Path

from hub.logger import get_logger
from hub.paths import BUNDLE_DIR

# 冻结后从 _MEIPASS/voice_clone 导入（打包内置）；源码运行为项目根下同目录
_VC_DIR = BUNDLE_DIR / "voice_clone"
if str(_VC_DIR) not in sys.path:
    sys.path.insert(0, str(_VC_DIR))

import minimax_client as _vc  # noqa: E402  voice_clone 客户端库（非包，按路径导入）
from minimax_client import MiniMaxError, MiniMaxVoiceClient  # noqa: E402

_QUEUE_MAX = 32
_FAIL_NOTIFY_THRESHOLD = 3


class _Sentinel:
    __slots__ = ()

    def __repr__(self):
        return "<tts-sentinel>"


_SENTINEL = _Sentinel()


class TTSError(Exception):
    pass


def _default_notifier(title: str, message: str) -> None:
    try:
        from plyer import notification
        notification.notify(title=title, message=message, timeout=5)
    except Exception:
        pass  # 通知不可用（缺依赖/无桌面环境）时静默，不阻塞直播链路


class TTSClient:
    def __init__(self, config: dict = None, stats=None, log=None,
                 client=None, notifier=None):
        cfg = config or {}
        tts = cfg.get("tts", {}) or {}
        self._voice_id = str(tts.get("voice_id", "") or "")
        self._model = str(tts.get("model", "") or "")
        self._speed = tts.get("speed", 1.0)
        self._vol = tts.get("vol", 1.0)
        self._pitch = tts.get("pitch", 0)
        # 语气情绪（speech-2.8-hd 支持）：空串 = 不设置，由模型按文案自动推断
        self._emotion = str(tts.get("emotion", "") or "")
        self._stats = stats
        self._log = log or get_logger("tts")
        self._notifier = notifier or _default_notifier
        self._consecutive_failures = 0
        self._client = client or self._build_client()

    def _build_client(self) -> MiniMaxVoiceClient:
        if not self._voice_id:
            raise TTSError("config 缺 tts.voice_id（克隆音色名，向导克隆后填写）")
        vc_cfg = _vc.load_config()
        api_key = str(vc_cfg.get("api_key", "") or "").strip()
        if not api_key:
            raise TTSError(
                "MiniMax API Key 未配置：voice_clone\\voice_clone_data\\config.json"
                " 里没有 api_key，控制台「音色工坊」可配置并热生效")
        endpoint = str(vc_cfg.get("endpoint", _vc.DEFAULT_ENDPOINT) or _vc.DEFAULT_ENDPOINT)
        model = self._model or _vc.DEFAULT_MODEL
        return MiniMaxVoiceClient(api_key, endpoint=endpoint, model=model)

    @property
    def voice_id(self) -> str:
        return self._voice_id

    async def synthesize_stream(self, text: str, emotion: str = None,
                               speed: float = None, vol: float = None,
                               pitch: int = None):
        """流式合成一条话术：逐块产出 PCM bytes（24000Hz int16 单声道）。

        `emotion` / `speed` / `vol` / `pitch` 为**逐条覆盖**（DESIGN §8.1：音色全场
        固定不变，情绪与语速音调按条动态化）；传 None = 沿用构造期配置，故旧调用点
        （Player 的默认流工厂）行为一字不变。emotion 传空串 = 本轮不设情绪。

        失败（MiniMaxError/网络错）在 async 侧原样抛出，由调用方
        （Player，步骤 4.3）捕获、丢弃该条并继续——本模块只负责计数
        与连续失败通知，不吞错。
        """
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        errors: list = []

        client = self._client
        voice_id, model = self._voice_id, self._model
        speed = self._speed if speed is None else speed
        vol = self._vol if vol is None else vol
        pitch = self._pitch if pitch is None else pitch
        emotion = self._emotion if emotion is None else str(emotion or "")

        def worker():
            try:
                for chunk in client.synthesize_stream(
                        text, voice_id=voice_id, model=model,
                        speed=speed, vol=vol, pitch=pitch,
                        emotion=emotion or None):
                    # 有界队列 + 阻塞式投递 = 天然背压（DESIGN §4.4）
                    asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
            except BaseException as e:
                errors.append(e)
            finally:
                # 哨兵必达；async 侧已死时带超时放弃（daemon 线程随进程退出）
                fut = asyncio.run_coroutine_threadsafe(queue.put(_SENTINEL), loop)
                try:
                    fut.result(timeout=15)
                except Exception:
                    pass

        threading.Thread(target=worker, daemon=True, name="tts-stream").start()

        try:
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                yield item
        finally:
            # 消费方提前退出（取消/aclose）时排空队列，放行 worker 的阻塞投递
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

        if errors:
            self._on_failure(errors[0])
            raise errors[0]
        self._on_success()
        # 合成字符量留痕（info）：日报成本核算的 TTS 消耗口径（步骤 6.2）
        self._log.info("tts_stream_done", {"chars": len(text),
                                           "emotion": emotion or "<默认>"})

    def _on_failure(self, error) -> None:
        if self._stats is not None:
            self._stats.inc("tts_fail")
        self._consecutive_failures += 1
        self._log.warning("tts_stream_failed", {
            "error": f"{type(error).__name__}: {error}",
            "consecutive": self._consecutive_failures,
        })
        if self._consecutive_failures >= _FAIL_NOTIFY_THRESHOLD:
            self._notify()
            self._consecutive_failures = 0

    def _on_success(self) -> None:
        self._consecutive_failures = 0

    def _notify(self) -> None:
        try:
            self._notifier("仔仔鼠 TTS 连续失败",
                           f"语音合成连续失败 {_FAIL_NOTIFY_THRESHOLD} 次，"
                           "请检查 MiniMax Key、配额或网络；失败的话术已丢弃。")
        except Exception as e:
            self._log.warning("tts_notify_failed", {"error": str(e)})


def save_wav(pcm: bytes, path, sample_rate: int = 24000, channels: int = 1) -> str:
    """把 PCM（int16 单声道）包 RIFF 头落盘，供验收试听。"""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return str(path)
