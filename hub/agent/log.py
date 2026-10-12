"""决策日志 JSONL（DESIGN §7.2 / §11.1）。

每次决策追加一行到 `logs/agent-YYYYMMDD.jsonl`，供 M5 的「决策透视」页读最近 500 条。
记录里同时保留**模型原始情绪**（`emotion_raw`）与**最终落地的情绪**（`emotion`）以及
回落事件名（`emotion_event`）——否则事后回看无法区分"模型没选对"和"我们改掉了"。

写失败绝不向上抛：日志是旁路观测，不能反噬决策链路（与 `hub.logger` 同一原则）。
`agent.decision_log: false` 时整体 no-op（连文件都不建）。
"""
import json
import threading
import time
from datetime import datetime
from pathlib import Path

from hub.logger import get_logger

DEFAULT_DIR = "logs"
FILE_PREFIX = "agent-"


class DecisionLogger:
    def __init__(self, log_dir=None, enabled: bool = True, clock=None, log=None):
        self._dir = Path(log_dir) if log_dir else Path(DEFAULT_DIR)
        self._enabled = bool(enabled)
        self._clock = clock or time.time
        self._log = log or get_logger("agent.log")
        self._lock = threading.Lock()
        self._day = ""
        self._path: Path | None = None
        if self._enabled:
            self._ensure()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def path(self) -> Path | None:
        """当日日志文件路径（未启用或目录不可写时为 None）。"""
        return self._path

    def set_enabled(self, enabled: bool) -> None:
        """热切换 `agent.decision_log`（切换后立即作用于下一次写入）。"""
        self._enabled = bool(enabled)
        if self._enabled:
            self._ensure()
        self._log.info("agent_decision_log_switched", {"enabled": self._enabled})

    def write(self, record: dict) -> None:
        """追加一行（自带 ts）；任何 IO 异常都吞掉并记 warning。"""
        if not self._enabled:
            return
        path = self._ensure()
        if path is None:
            return
        payload = {"ts": datetime.fromtimestamp(self._clock()).isoformat(
            timespec="milliseconds")}
        if isinstance(record, dict):
            payload.update(record)
        else:
            payload["detail"] = str(record)
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as e:
                self._log.warning("agent_decision_log_write_failed", {"error": str(e)})

    def read_recent(self, limit: int = 500) -> list:
        """读当日日志最后 limit 条（解析不了的坏行跳过，不抛异常）。"""
        path = self.path()
        if path is None or not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for raw in lines[-limit:]:
            try:
                out.append(json.loads(raw))
            except ValueError:
                continue
        return out

    def _ensure(self) -> Path | None:
        """按天滚动；目录不可写时置 None（后续写入静默丢弃）。"""
        try:
            day = datetime.fromtimestamp(self._clock()).strftime("%Y%m%d")
        except (OSError, OverflowError, ValueError):
            return None
        if day == self._day and self._path is not None:
            return self._path
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            if self._path is None:
                self._log.warning("agent_decision_log_dir_failed", {"error": str(e)})
            self._path = None
            return None
        self._day = day
        self._path = self._dir / f"{FILE_PREFIX}{day}.jsonl"
        return self._path


def summarize(records) -> dict:
    """决策透视的四张聚合卡（DESIGN §7.2④）：本场决策数 / 平均延迟 / 不回比例 / 各类占比。

    口径的两条硬规矩（V5.2「聚合卡数值与日志实际条数一致」靠它守住）：

    1. **`test: true` 的记录（控制台测试事件）不计入本场决策数**，单独报 `test_skipped`。
       把它们混进"本场决策 128 条"会让主播以为智能体干了很多活，实际是自己在点测试按钮。
    2. **降级与预录音频单独计数**：`degraded` 是"模型没按契约输出、被容错链救回来"的次数，
       `prerecorded` 是"复用了预录音频、没花现场合成时间"的次数——这两个数字才是
       "有没有问题 / 省了多少延迟"的真实信号，不该埋在明细里。
    """
    items = list(records or [])
    live = [r for r in items if not r.get("test")]
    replied = [r for r in live if r.get("should_reply")]
    latencies = [int(r.get("latency_ms") or 0) for r in live]

    by_type: dict = {}
    for r in live:
        kind = str(r.get("type") or "unknown")
        by_type[kind] = by_type.get(kind, 0) + 1
    total = len(live)
    ratio = (lambda n: round(n / total * 100, 1)) if total else (lambda n: 0.0)

    return {
        "total": total,
        "test_skipped": len(items) - total,
        "replied": len(replied),
        "no_reply": total - len(replied),
        "no_reply_ratio": ratio(total - len(replied)),
        "avg_latency_ms": (int(round(sum(latencies) / len(latencies)))
                           if latencies else 0),
        "by_type": by_type,
        "by_type_ratio": {k: ratio(v) for k, v in by_type.items()},
        "degraded": sum(1 for r in live if r.get("degrade")),
        "prerecorded": sum(1 for r in live if r.get("prerecorded")),
        "first_ts": (live[0].get("ts") if live else ""),
        "last_ts": (live[-1].get("ts") if live else ""),
    }


__all__ = ["DEFAULT_DIR", "FILE_PREFIX", "DecisionLogger", "summarize"]

