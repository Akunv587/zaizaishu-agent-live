"""定时口播调度（PROMPTS 步骤 5.1，DESIGN §4.6；PRD FR-4；v3.1 秒级话题轮播）。

独立 asyncio 协程：等待 interval 秒 ± jitter 随机抖动 →
从轮播条目取一条（避开最近播过的 2 条）→ 构造 priority=2、source="schedule"
的任务（条目带预录音频时 SpeakTask.audio_path 指向存档音频，播放侧优先播文件）
→ queue.put_idle：P0/P1 积压时返回 False，本轮放弃不占队（"空闲才说话"）。

供料两态：旧 config.schedule（interval_min 分钟 + pool 纯文本，构造时映射）；
v3.1 话术存档话题（configure 热切换，秒级 interval_s/jitter_s + 音频条目）。
话术直接来自条目，不走 LLM。暂停期间（FR-5）本轮跳过不出任务。
确定性测试：clock / random / sleep 均为构造注入。
"""
import asyncio
import random as _random
import time

from hub.events import SpeakTask
from hub.logger import get_logger

JITTER_S = 60.0        # 旧配置默认抖动幅度 ±60 秒（PROMPTS 步骤 5.1）
AVOID_RECENT_N = 2     # 避开最近播过的 2 条


def _normalize_items(items) -> list:
    """轮播条目归一：[{"text","audio_path"}]，文本与音频全空的丢弃。"""
    out = []
    for it in items or []:
        if isinstance(it, str):
            it = {"text": it}
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()
        audio = str(it.get("audio_path") or "").strip()
        if text or audio:
            out.append({"text": text, "audio_path": audio})
    return out


class Scheduler:
    def __init__(self, queue, config: dict = None, stats=None, log=None,
                 clock=None, random=None, sleep=None):
        cfg = (config or {}).get("schedule", {}) or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._interval_s = float(cfg.get("interval_min", 7)) * 60.0
        self._jitter_s = JITTER_S
        self._items = _normalize_items(cfg.get("pool", []) or [])
        self._queue = queue
        self._stats = stats
        self._log = log or get_logger("scheduler")
        self._clock = clock or time.time
        self._random = random or _random
        self._sleep = sleep or asyncio.sleep
        self._ptr = 0            # 轮换指针（下次从条目中取的位置）
        self._recent: list[str] = []  # 最近实际播过的条目（最多 2 条）
        self._announced = 0
        self._yielded = 0
        self._live = False      # 是否已「开始直播」：未开始不触发话题轮播（其余供料不受影响）
        self._reply_busy = None # () -> bool：有弹幕回复在生成/排队时 True（main 注入，让位用）
        # 长睡唤醒信号：configure/set_live 打断进行中的 interval 睡眠——
        # 否则切存档/改节奏后要等旧周期睡完才生效（极端 420s，表现像「轮播不工作」）
        self._wake = asyncio.Event()

    # ---------- 对外接口 ----------

    def configure(self, enabled: bool = None, interval_s=None, jitter_s=None,
                  items=None) -> None:
        """热切换轮播配置（v3.1 话术存档）：None 的字段保持现值；items 更换时重置轮换。"""
        if enabled is not None:
            self._enabled = bool(enabled)
        if interval_s is not None:
            self._interval_s = max(float(interval_s), 1.0)
        if jitter_s is not None:
            self._jitter_s = max(float(jitter_s), 0.0)
        if items is not None:
            self._items = _normalize_items(items)
            self._ptr = 0
            self._recent = []
        self._log.info("scheduler_reconfigured", {
            "enabled": self._enabled, "interval_s": self._interval_s,
            "jitter_s": self._jitter_s, "items": len(self._items)})
        self._wake.set()  # 打断进行中的长睡，让新配置下一轮循环立即生效

    def stats(self) -> dict:
        return {"announced": self._announced, "yielded": self._yielded}

    @property
    def is_live(self) -> bool:
        return self._live

    def set_live(self, live: bool) -> None:
        """开始/结束直播门控：未开始直播时话题轮播空转不产出（1s 心跳待恢复）。"""
        self._live = bool(live)
        self._log.info("scheduler_live_changed", {"live": self._live})
        self._wake.set()  # 打断长睡：结束直播时睡醒的那一觉不得再播

    def set_reply_busy(self, cb) -> None:
        """注入「弹幕回复处理中」探针：True 时话题轮播让位等待（不占队、不抢占）。"""
        self._reply_busy = cb

    async def run(self, stop: asyncio.Event = None) -> None:
        if stop is None:
            stop = asyncio.Event()
        if not self._enabled:
            self._log.info("scheduler_disabled")
            return
        if not self._items:
            self._log.warning("scheduler_empty_pool")
            return
        self._log.info("scheduler_started", {
            "interval_s": self._interval_s, "jitter_s": self._jitter_s,
            "items": len(self._items)})
        try:
            while not stop.is_set():
                if not self._live or not self._enabled or not self._items:
                    # 未开始直播 / 热停用 / 条目清空：低频率空转待恢复
                    await self._sleep(1.0)
                    continue
                outcome = await self._wait_or_woken(self._next_delay_s())
                if stop.is_set():
                    break
                if outcome == "woken":
                    # configure/set_live 打断了长睡：回主循环按最新配置与门控重算
                    continue
                if not self._live:
                    # 睡觉期间被「结束直播」：本轮不播（门控复查，防睡醒漏播一条）
                    continue
                if getattr(self._queue, "paused", False):
                    self._log.info("idle_skipped_paused")
                    continue
                # 弹幕回复让位：有回复在生成/排队/合并时，等其结束再进下一条话题
                if self._reply_busy is not None:
                    while self._reply_busy():
                        if not self._live or stop.is_set():
                            break
                        await self._sleep(0.5)
                    if stop.is_set():
                        break
                    if not self._live:
                        continue
                await self._emit_once()
        except asyncio.CancelledError:
            pass
        self._log.info("scheduler_stopped", self.stats())

    async def _wait_or_woken(self, delay: float) -> str:
        """睡满 delay 返回 "elapsed"；期间 configure/set_live 打断返回 "woken"。

        注入的假 sleep 仍是单次完整调用（delays 断言不变）；被打断时挂起的
        sleep task 取消收尾，避免事件循环遗留任务。
        """
        if delay <= 0:
            return "elapsed"
        sleep_task = asyncio.ensure_future(self._sleep(delay))
        wake_task = asyncio.ensure_future(self._wake.wait())
        try:
            done, _pending = await asyncio.wait(
                {sleep_task, wake_task}, return_when=asyncio.FIRST_COMPLETED)
            return "elapsed" if sleep_task in done else "woken"
        finally:
            for t in (sleep_task, wake_task):
                if not t.done():
                    t.cancel()
            await asyncio.gather(sleep_task, wake_task, return_exceptions=True)
            self._wake.clear()

    # ---------- 内部 ----------

    def _next_delay_s(self) -> float:
        jitter = self._random.uniform(-self._jitter_s, self._jitter_s)
        return max(self._interval_s + jitter, 1.0)

    def _next_item(self) -> dict:
        """轮换指针顺序取下一条，优先避开最近播过的 2 条（条目过少退化为普通轮换）。

        仅移动指针不登记 recent——recent 只在真正入队成功后更新（让位放弃的
        条目不算"用过"，下一轮仍优先取它）。
        """
        n = len(self._items)
        candidates = [(self._ptr + i) % n for i in range(n)]
        fresh = [i for i in candidates if self._identity(self._items[i]) not in self._recent]
        idx = (fresh or candidates)[0]
        self._ptr = (idx + 1) % n
        return self._items[idx]

    @staticmethod
    def _identity(item: dict) -> str:
        return item["text"] or item["audio_path"]

    async def _emit_once(self) -> None:
        item = self._next_item()
        task = SpeakTask(priority=2, text=item["text"], event_ts=self._clock(),
                         source="schedule", audio_path=item["audio_path"])
        ok = await self._queue.put_idle(task)
        if ok:
            self._announced += 1
            if self._stats is not None:
                self._stats.inc("idle_announced")
                self._stats.inc("task_produced")  # 口播也是入队任务，统一产出口径
            self._recent.append(self._identity(item))
            self._recent = self._recent[-AVOID_RECENT_N:]
            self._log.info("idle_task_enqueued", {"text": item["text"],
                                                  "audio": bool(item["audio_path"])})
        else:
            self._yielded += 1
            if self._stats is not None:
                self._stats.inc("idle_yielded")
            self._log.info("idle_task_yielded", {"text": item["text"]})
