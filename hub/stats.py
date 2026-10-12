"""运行统计与延迟采样（PROMPTS 步骤 0.2）。

计数器：danmaku_in / reply_generated / played / dropped_stale /
dropped_pause（F9 暂停清空的积压）/ blocked_input / blocked_output /
llm_fail / tts_fail。
自洽口径（步骤 5.3 验收）：task_produced = played + dropped_stale +
dropped_pause + 关停时在途（应为 0）。
线程安全：TTS 工作线程会调用 tts_fail 计数。
"""
import threading
import time

_COUNTERS = (
    "danmaku_in",
    "reply_generated",
    "played",
    "dropped_stale",
    "dropped_pause",
    "blocked_input",
    "blocked_output",
    "llm_fail",
    "tts_fail",
)


class StatsCollector:
    def __init__(self):
        self._counters = {name: 0 for name in _COUNTERS}
        self._lock = threading.Lock()
        self._latencies: list[float] = []
        self._extra: dict[str, int] = {}

    def inc(self, name: str, n: int = 1) -> None:
        with self._lock:
            if name in self._counters:
                self._counters[name] += n
            else:
                self._extra[name] = self._extra.get(name, 0) + n

    def record_latency(self, seconds: float) -> None:
        with self._lock:
            self._latencies.append(float(seconds))

    def get(self, name: str) -> int:
        with self._lock:
            return self._counters.get(name, self._extra.get(name, 0))

    def reset(self, name: str) -> int:
        """清零计数器并返回清零前的值（看门狗失败告警后复位用，步骤 5.2）。"""
        with self._lock:
            if name in self._counters:
                old = self._counters[name]
                self._counters[name] = 0
                return old
            old = self._extra.get(name, 0)
            if name in self._extra:
                self._extra[name] = 0
            return old

    def summary(self) -> dict:
        with self._lock:
            result = dict(self._counters)
            result.update(self._extra)
            if self._latencies:
                result["latency_avg_s"] = round(sum(self._latencies) / len(self._latencies), 3)
                result["latency_max_s"] = round(max(self._latencies), 3)
            else:
                result["latency_avg_s"] = None
                result["latency_max_s"] = None
            result["latency_samples"] = len(self._latencies)
            return result

    def format_summary(self) -> str:
        s = self.summary()
        if s["latency_samples"] > 0:
            latency = f"平均 {s['latency_avg_s']}s / 最大 {s['latency_max_s']}s（{s['latency_samples']} 次采样）"
        else:
            latency = "暂无采样"
        lines = [
            "── 运行统计 ─────────────────────────────",
            f"弹幕事件: {s['danmaku_in']}  生成回复: {s['reply_generated']}  播出: {s['played']}  过发作废: {s['dropped_stale']}  暂停清理: {s['dropped_pause']}",
            f"输入拦截: {s['blocked_input']}  输出拦截: {s['blocked_output']}  LLM失败: {s['llm_fail']}  TTS失败: {s['tts_fail']}",
            f"端到端延迟: {latency}",
            "─────────────────────────────────────────",
        ]
        return "\n".join(lines)
