"""看门狗与桌面通知（PROMPTS 步骤 5.2，DESIGN §4.8；PRD FR-7）。

独立协程每 30 秒巡检失败计数：stats 的 llm_fail / tts_fail 超过阈值
（默认 10 次）时汇总一次桌面通知并把该计数复位（下一批失败重新累计
才会再报）。启动后即开始巡检。

通知默认实现：控制台醒目打印 + plyer 桌面通知（通知不可用时静默，不
阻塞直播链路）。确定性测试：clock / sleep / notify 均构造注入，
interval_s 仅测试与验收脚本压缩巡检用，线上固定 30 秒。
"""
import asyncio
import time

from hub.logger import get_logger, safe_print

PATROL_INTERVAL_S = 30.0            # 巡检周期（DESIGN §4.8 固定值）
FAIL_THRESHOLD_DEFAULT = 10
FAIL_COUNTERS = ("llm_fail", "tts_fail")
FAIL_LABELS = {"llm_fail": "LLM失败", "tts_fail": "TTS失败"}


def default_notify(title: str, message: str) -> None:
    safe_print(f"[看门狗] {title}：{message}")
    try:
        from plyer import notification
        notification.notify(title=title, message=message, timeout=5)
    except Exception:
        pass  # 通知不可用（缺依赖/无桌面环境）时静默，控制台打印已兜底


class Watchdog:
    def __init__(self, queue, stats=None, config: dict = None, log=None,
                 clock=None, sleep=None, notify=None, interval_s=None):
        cfg = (config or {}).get("watchdog", {}) or {}
        self._fail_threshold = int(cfg.get("fail_threshold", FAIL_THRESHOLD_DEFAULT))
        self._interval_s = float(PATROL_INTERVAL_S if interval_s is None else interval_s)
        self._queue = queue
        self._stats = stats
        self._log = log or get_logger("watchdog")
        self._clock = clock or time.time
        self._sleep = sleep or asyncio.sleep
        self._notify = notify or default_notify
        self._fail_alerts = 0

    # ---------- 对外接口 ----------

    def stats(self) -> dict:
        return {"fail_alerts": self._fail_alerts}

    async def run(self, stop: asyncio.Event = None) -> None:
        if stop is None:
            stop = asyncio.Event()
        self._log.info("watchdog_started", {
            "fail_threshold": self._fail_threshold,
            "interval_s": self._interval_s,
        })
        try:
            while not stop.is_set():
                await self._sleep(self._interval_s)
                if stop.is_set():
                    break
                self._check_failures()
        except asyncio.CancelledError:
            pass
        self._log.info("watchdog_stopped", self.stats())

    # ---------- 内部 ----------

    def _check_failures(self) -> None:
        if self._stats is None:
            return
        exceeded = []
        for name in FAIL_COUNTERS:
            value = self._stats.get(name)
            if value >= self._fail_threshold:
                exceeded.append((name, self._stats.reset(name)))
        if not exceeded:
            return
        self._fail_alerts += len(exceeded)
        detail = "、".join(f"{FAIL_LABELS.get(n, n)} {v} 次" for n, v in exceeded)
        self._notify(
            "仔仔鼠·失败计数告警",
            f"近期{detail}（阈值 {self._fail_threshold} 次），相关计数已复位。"
            f"请检查网络、密钥与配额。")
        self._log.info("watchdog_fail_alert", {
            "counters": {n: v for n, v in exceeded},
            "threshold": self._fail_threshold,
        })