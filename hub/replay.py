"""JSONL 重放事件源（PROMPTS 步骤 2.1，DESIGN §4.1）。

与 WS 源共用同一 on_event 回调接口：逐行读取 Dycast 录制的 JSONL
（每行一条带 timestamp 毫秒的 DyMessage；首行直播间信息对象跳过），
按相邻时间戳差值 / speed 睡眠后经 normalize 产出 LiveEvent。
"""
import asyncio
import inspect
import json
import time

from hub.events import LiveEvent, normalize
from hub.logger import get_logger


class ReplaySource:
    def __init__(self, path, on_event, speed: float = 1.0, log=None, clock=None):
        self._path = path
        self._on_event = on_event
        self._speed = max(float(speed), 0.01)
        self._log = log or get_logger("replay")
        self._clock = clock or time.time
        self._counters: dict[str, int] = {}
        self._total = 0
        self._duration_ms = 0.0

    def stats(self) -> dict:
        return {
            "file": str(self._path),
            "total_events": self._total,
            "by_type": dict(self._counters),
            "duration_s": round(self._duration_ms / 1000.0, 1),
            "speed": self._speed,
        }

    def _load_lines(self):
        """读文件，产出 (ts_ms, dy_dict) 序列，保持文件顺序。"""
        rows = []
        last_ts = 0.0
        with open(self._path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError as e:
                    self._log.warning("replay_line_parse_error", {"line": line_no, "error": str(e)})
                    continue

                ts_ms = _extract_ts_ms(data, line_no, self._log)
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if "method" not in item:
                        # 直播间信息对象（首行常见）——跳过并记录一次
                        self._log.debug("replay_skip_live_info", {"line": line_no})
                        continue
                    # 缺时间戳的行沿用最近一次已知时间戳，避免 0 与真实时间戳之间
                    # 产生天文数字级 sleep（Dycast 正常每行都带 timestamp）
                    item_ts = _extract_ts_ms(item, line_no, self._log) or ts_ms
                    if item_ts <= 0:
                        item_ts = last_ts
                    if item_ts > 0:
                        last_ts = item_ts
                    rows.append((item_ts, item))
        return rows

    async def run(self):
        """按原始时间间隔回放全部事件，结束后输出统计。"""
        rows = self._load_lines()
        if not rows:
            self._log.warning("replay_empty", {"file": str(self._path)})
            return

        first_ts, last_ts = rows[0][0], rows[-1][0]
        self._duration_ms = (last_ts - first_ts) if last_ts >= first_ts else 0.0
        prev_ts = None

        self._log.info("replay_started", {
            "file": str(self._path), "rows": len(rows),
            "duration_s": round(self._duration_ms / 1000.0, 1), "speed": self._speed,
        })

        for ts_ms, dy in rows:
            if prev_ts is not None:
                delay = (ts_ms - prev_ts) / 1000.0 / self._speed
                if delay > 0:
                    await asyncio.sleep(delay)
            prev_ts = ts_ms
            ev = normalize(dy, ts=self._clock())
            if ev is not None:
                self._total += 1
                self._counters[ev.type] = self._counters.get(ev.type, 0) + 1
                await self._emit(ev)

        summary = self.stats()
        self._log.info("replay_finished", summary)
        print(
            f"重放完成：共 {summary['total_events']} 条事件 "
            f"{summary['by_type']}，原始时长 {summary['duration_s']} 秒（speed={summary['speed']}）"
        )

    async def _emit(self, ev: LiveEvent):
        result = self._on_event(ev)
        if inspect.isawaitable(result):
            await result


def _extract_ts_ms(data, line_no: int, log) -> float:
    """从行对象提取毫秒时间戳；兼容 timestamp/ts 字段与秒级数值。"""
    if not isinstance(data, dict):
        return 0.0
    raw = data.get("timestamp", data.get("ts"))
    if raw is None:
        log.debug("replay_line_no_timestamp", {"line": line_no})
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    # 秒级时间戳（约 10 位）转毫秒
    if value < 1e12:
        value *= 1000.0
    return value
