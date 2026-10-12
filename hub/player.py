"""播放调度器与声卡输出（PROMPTS 步骤 4.3，DESIGN §4.5/§6.3；PRD FR-1/FR-5）。

串行播放：queue.get() 出队（过期作废已由队列侧处理）→ TTS 流式合成 →
首块到达即打开 sounddevice RawOutputStream（device/samplerate/channels 读
config audio 组，dtype int16）写入 → 后续块到达即写 → 流尽补 gap_ms 停顿
→ 取下一条。一条播完对 stats 的 played 计数并记录端到端延迟
（event_ts → 首块写入）。

状态机（DESIGN §6.3）：待机 idle / 播放 playing / 暂停 paused。暂停由外部
置位（步骤 4.4 热键同时调 queue.pause() 与 player.pause()）：**立即中断当前
句**（cancel 播放协程并关流，不再是「播完停」）；resume() 后从新任务继续。
被中断的那句连同队列积压一起丢弃，恢复后不补播。

容错：单条 TTS 抛错捕获、记日志、继续下一条（tts_fail 已由 TTSClient 计数，
不重复）；启动时找不到 config 指定的输出设备，抛 PlayerError 并附本机全部
可用输出设备名。

情绪分派（v4，DESIGN §8.3）：任务的 audio_path 已由 selector 按「音色 × 情绪档」
三级回落解析好——命中档位或回落 default 的都有文件，直接播；解析为空串说明
该情绪没预生成，本条现场 TTS 并带上 task.emotion/task.speed 覆盖（记
`emotion_onsite` 留痕）。
"""
import asyncio
import time

from hub.logger import get_logger, safe_print
from hub.scriptstore import EMOTION_DEFAULT


class PlayerError(Exception):
    """输出设备不可用等启动期错误（消息含可用设备清单）。"""


class Player:
    def __init__(self, queue, tts, stats=None, log=None, config=None,
                 stream_factory=None, out_factory=None, clock=None):
        cfg = config or {}
        audio = cfg.get("audio", {}) or {}
        self._queue = queue
        self._tts = tts
        self._stats = stats
        self._log = log or get_logger("player")
        self._device = str(audio.get("device", "") or "")
        self._sample_rate = int(audio.get("sample_rate", 24000))
        self._channels = int(audio.get("channels", 1))
        self._gap_s = max(float(audio.get("gap_ms", 800)) / 1000.0, 0)
        self._clock = clock or time.time

        self._state = "idle"
        self._current_task = None
        self._resume_evt = asyncio.Event()
        self._resume_evt.set()
        self._play_task_handle = None  # 当前播放协程句柄（暂停时 cancel 以立即中断）

        # 测试注入位：stream_factory(text)->async iterator；out_factory()->输出流
        self._stream_factory = stream_factory or (
            lambda text: self._tts.synthesize_stream(text))
        self._out_factory = out_factory

        # 可视化控制台挂钩（hub.webui 注入）：首块到达/整条播完时回调
        self.on_play_start = None
        self.on_play_end = None
        # 设备解析失败回退系统默认输出时回调（webui 注入，事件流可见提示用）
        self.on_device_fallback = None

    # ---------- 对外接口 ----------

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_playing(self) -> bool:
        return self._current_task is not None

    def current_info(self) -> dict | None:
        """控制台快照用：当前在播任务概要（无则 None）。"""
        task = self._current_task
        if task is None:
            return None
        return {"source": task.source, "priority": task.priority, "text": task.text,
                "tier": task.tier, "merged_count": task.merged_count}

    def check_device(self) -> None:
        """启动期设备校验：设备名不在本机输出列表时抛 PlayerError（含清单）。"""
        if self._out_factory is not None:
            return  # 测试注入路径不碰真实声卡
        from hub.webui import resolve_output_device, _list_output_devices
        if not self._device:
            return  # 留空走系统默认输出
        try:
            resolve_output_device(self._device)
        except ValueError:
            outputs = _list_output_devices(refresh=True)
            raise PlayerError(
                f"找不到配置的输出设备「{self._device}」，请核对 config.yaml 的 "
                f"audio.device 拼写。\n本机可用输出设备：\n"
                + "\n".join(f"  - {n}" for n in outputs))

    def swap_tts(self, tts) -> None:
        """热替换 TTS 客户端（音色工坊保存 Key / 切换直播音色后无需重启）。

        _stream_factory 的 lambda 运行时取 self._tts（延迟求值），故仅换引用即生效。
        """
        self._tts = tts
        self._log.info("player_tts_swapped", {"voice": getattr(tts, "voice_id", "?")})

    def swap_device(self, device: str) -> None:
        """热切换输出声卡（控制台选择后立即生效，下一条话术走新设备）。

        当前在播的一条不受影响（流已建立）；切换仅影响后续打开的流。
        """
        old = self._device
        self._device = str(device or "")
        self._log.info("player_device_swapped", {"from": old, "to": self._device})

    async def run(self, stop: asyncio.Event = None) -> None:
        """主循环：串行播放，无任务时安静挂起，不空转烧 CPU。"""
        if stop is None:
            stop = asyncio.Event()
        self._log.info("player_started", {
            "device": self._device or "<系统默认>", "sample_rate": self._sample_rate,
            "channels": self._channels, "gap_ms": int(self._gap_s * 1000)})
        while not stop.is_set():
            await self._resume_evt.wait()  # 暂停中挂起（当前句已被立即中断）
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue  # 空闲轮询（0.5s 一次唤醒，近乎零开销）
            self._play_task_handle = asyncio.create_task(self._play_task(task))
            try:
                await self._play_task_handle
            except asyncio.CancelledError:
                pass  # 暂停触发的立即中断：声音已停，本条丢弃不补播
            finally:
                self._play_task_handle = None
            await asyncio.sleep(self._gap_s)  # 条间停顿（DESIGN §4.5）
        self._log.info("player_stopped", {"state": self._state})

    async def pause(self) -> None:
        """FR-5：立即中断当前句（cancel 播放协程 + 关流）；积压清理由热键层调 queue.pause()。"""
        if self._state == "paused":
            return
        self._state = "paused"
        self._resume_evt.clear()
        if self._play_task_handle is not None and not self._play_task_handle.done():
            self._play_task_handle.cancel()  # 立即中断：_play_stream 的 finally 会关流
        self._log.info("player_paused")

    async def resume(self) -> None:
        if self._state != "paused":
            return
        self._state = "idle"
        self._resume_evt.set()
        self._log.info("player_resumed")

    # ---------- 内部 ----------

    async def _play_task(self, task) -> None:
        """单条任务：task.audio_path 非空优先播预录音频（v4 话术存档：音色×情绪档），
        解码失败回退 TTS 念 task.text（降级链不断声）。"""
        if getattr(task, "audio_path", ""):
            try:
                pcm = self._decode_wav(task.audio_path)  # 同步预解码：错误在此抛出
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log.warning("audio_file_play_failed", {
                    "source": task.source, "audio": task.audio_path,
                    "error": f"{type(e).__name__}: {e}"})
                safe_print(f"[音频失败回退TTS] {type(e).__name__}: {e}")
                if self._stats is not None:
                    self._stats.inc("audio_file_fail")
                if not task.text:
                    return  # 无文本可念，本条放弃
            else:
                await self._play_stream(task, self._pcm_chunks(pcm))
                return
        await self._play_stream(task, self._make_stream(task))

    def _make_stream(self, task):
        """现场合成流：带任务的逐条情绪/语速覆盖（DESIGN §8.1/§8.3）。

        未命中预录音频时才会走到这里。任务带**非 default** 情绪覆盖时，本条属
        「未预生成档位 → 现场 TTS」，记 `emotion_onsite` 留痕（V2.5 验收点）；
        无覆盖（空 / default，且无语速覆盖）时沿用注入的流工厂——旧调用点与测试
        行为一字不变，`default` 是我们内部的兜底档名而非 MiniMax 情绪取值，
        绝不透传给合成接口。
        """
        emotion = str(getattr(task, "emotion", "") or "").strip()
        speed = getattr(task, "speed", None)
        override = emotion if emotion and emotion != EMOTION_DEFAULT else None
        if override is None and speed is None:
            return self._stream_factory(task.text)
        if override:
            if self._stats is not None:
                self._stats.inc("emotion_onsite")
            self._log.info("emotion_onsite", {
                "source": task.source, "emotion": override,
                "chars": len(str(task.text or ""))})
        try:
            return self._tts.synthesize_stream(task.text,
                                               emotion=override, speed=speed)
        except TypeError:
            # 注入的 TTS 实现不支持逐条覆盖：退回无参调用，不让播放链路断声
            self._log.debug("tts_emotion_override_unsupported",
                            {"source": task.source, "emotion": override})
            return self._stream_factory(task.text)

    def _decode_wav(self, path: str) -> bytes:
        """预录音频 → 整块 PCM（本地小文件无 IO 顾虑）。

        仅接受与输出流同参的 24000Hz 单声道 16bit WAV——页面上传的音频已在前端
        转码为该格式；手工导入的 mp3/m4a/异参 wav 在此报错，由 _play_task 回退 TTS。
        """
        import wave

        with wave.open(str(path), "rb") as wf:
            if (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) != (
                    self._sample_rate, 1, 2):
                raise ValueError(
                    f"仅支持 {self._sample_rate}Hz 单声道 16bit WAV"
                    f"（当前 {wf.getframerate()}Hz/{wf.getnchannels()}声道/"
                    f"{wf.getsampwidth() * 8}bit）——请在话术管理页重新上传该音频")
            pcm = wf.readframes(wf.getnframes())
        if not pcm:
            raise ValueError("音频文件没有有效声音数据")
        return pcm

    @staticmethod
    async def _pcm_chunks(pcm: bytes, block: int = 4800):
        """整块 PCM 切 100ms 块的异步迭代器（4800B = 24000Hz×2B×0.1s）。"""
        for i in range(0, len(pcm), block):
            yield pcm[i:i + block]

    async def _play_stream(self, task, chunk_iter) -> None:
        self._state = "playing"
        self._current_task = task
        stream = None
        chunks = 0
        extra = f" 合并{task.merged_count}" if task.merged_count > 1 else ""
        tier = f" 档位{task.tier}" if task.tier else ""
        try:
            async for chunk in chunk_iter:
                if stream is None:
                    stream = self._open_stream()
                    started = self._clock()
                    if self._stats is not None:
                        self._stats.record_latency(started - task.event_ts)
                    self._log.info("play_started", {
                        "source": task.source, "priority": task.priority,
                        "first_chunk_s": round(started - task.event_ts, 2)})
                    # 话术文本可含 emoji（GBK 控制台裸 print 会炸断本条播放），必须兜底
                    safe_print(f"[播放] P{task.priority} src={task.source}{extra}{tier} 「{task.text}」")
                    if self.on_play_start is not None:
                        self.on_play_start(task)
                # RawOutputStream.write 同步阻塞约一个块时长（100ms）；整条预制音频
                # 连续写会冻住事件循环数十秒（路由器/心跳/webui 全停摆、抖音断连），
                # 必须丢给线程执行让出循环。取消时正在飞行的一块最多多写 100ms，无害。
                await asyncio.to_thread(stream.write, chunk)
                chunks += 1
            if chunks > 0 and self._stats is not None:
                self._stats.inc("played")
            self._log.info("play_finished", {
                "source": task.source, "chunks": chunks})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 单条失败不中断主循环；tts_fail 已由 TTSClient 计数，此处只留痕
            self._log.warning("play_task_failed", {
                "source": task.source, "error": f"{type(e).__name__}: {e}",
                "chunks_before_error": chunks})
            safe_print(f"[播放失败] {task.source}: {type(e).__name__}: {e}")
        finally:
            if stream is not None:
                await self._close_stream(stream)
            self._current_task = None
            if self.on_play_end is not None:
                self.on_play_end(task)
            if self._state == "playing":
                self._state = "idle"

    def _open_stream(self):
        if self._out_factory is not None:
            stream = self._out_factory()
        else:
            import sounddevice as sd
            from hub.webui import resolve_output_device
            device_idx = None
            if self._device:
                try:
                    device_idx = resolve_output_device(self._device)
                except ValueError:
                    # 找不到配置设备（蓝牙耳机关机/设备被拔等）会静默落到系统默认
                    # 输出，用户感知为「切换没生效」——日志+控制台事件流双通道可见
                    self._log.warning("output_device_missing_fallback",
                                      {"device": self._device})
                    if self.on_device_fallback is not None:
                        try:
                            self.on_device_fallback(self._device)
                        except Exception:
                            pass  # 提示通道故障不挡播放
            stream = sd.RawOutputStream(
                samplerate=self._sample_rate, channels=self._channels,
                dtype="int16", device=device_idx)
        stream.start()
        return stream

    async def _close_stream(self, stream) -> None:
        # 暂停/关停取消播放时，最后一块 write 可能仍在 to_thread 线程里飞（≤100ms），
        # 此时 stop/close 会撞出 paStreamIsStopped(-9983) 一类错——属关停常态，
        # 只记 debug，绝不能刷 warning 吓用户
        try:
            stream.stop()
        except Exception as e:
            self._log.debug("stream_stop_skipped", {"error": str(e)})
        try:
            await asyncio.sleep(0.05)  # 让输出缓冲最后一点 PCM 播完再关，防截尾
            stream.close()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log.debug("stream_close_skipped", {"error": str(e)})
