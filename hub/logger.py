"""结构化 JSON 日志：每行一个 JSON 对象，按天滚动，控制台镜像（PROMPTS 步骤 0.2）。

字段：ts / module / level / event / detail。
文件：logs/live-YYYYMMDD.log，日期跨天自动切换。
线程安全：pynput 与 TTS 线程也会写日志。
"""
import json
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40}

_LOCK = threading.Lock()
_LOG_DIR = Path("logs")
_MIN_LEVEL = "info"


def setup(log_dir: Path, level: str = "info") -> None:
    """初始化日志目录与最低输出级别（main 启动时调用一次）。"""
    global _LOG_DIR, _MIN_LEVEL
    _LOG_DIR = Path(log_dir)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _MIN_LEVEL = str(level).lower()


def _now() -> datetime:
    return datetime.now()


def safe_print(text: str, file=None) -> None:
    """打印兜底：GBK 控制台遇到 emoji/生僻字会抛 UnicodeEncodeError——
    日志镜像与提示打印失败绝不能反噬事件处理、播放等正链路。"""
    try:
        print(text, file=file)
    except Exception:
        pass


def _write(module: str, level: str, event: str, detail) -> None:
    if _LEVELS.get(level, 20) < _LEVELS.get(_MIN_LEVEL, 20):
        return
    ts = _now()
    record = {
        "ts": ts.isoformat(timespec="milliseconds"),
        "module": module,
        "level": level,
        "event": event,
        "detail": detail if isinstance(detail, (dict, list, str, int, float, bool, type(None))) else str(detail),
    }
    line = json.dumps(record, ensure_ascii=False)
    day = ts.strftime("%Y%m%d")
    path = _LOG_DIR / f"live-{day}.log"
    human = f"{ts.strftime('%H:%M:%S')} [{module}] {level.upper():7s} {event} {_brief(detail)}"
    with _LOCK:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            safe_print(f"日志写入失败: {e}", file=sys.stderr)
        # 控制台镜像必须防御：GBK 控制台打 emoji 抛 UnicodeEncodeError，曾顺
        # on_event→logger 一路炸掉 router.handle_event，emoji 昵称弹幕全部静默丢失
        safe_print(human)


def _brief(detail) -> str:
    if detail is None:
        return ""
    if isinstance(detail, str):
        return detail
    text = json.dumps(detail, ensure_ascii=False)
    return text if len(text) <= 200 else text[:200] + "…"


class Logger:
    """按模块命名的日志器：log.info("chat_received", {"user": "张三"})。"""

    def __init__(self, module: str):
        self._module = module

    def debug(self, event: str, detail=None) -> None:
        _write(self._module, "debug", event, detail)

    def info(self, event: str, detail=None) -> None:
        _write(self._module, "info", event, detail)

    def warning(self, event: str, detail=None) -> None:
        _write(self._module, "warning", event, detail)

    def error(self, event: str, detail=None) -> None:
        _write(self._module, "error", event, detail)

    def exception(self, event: str, detail=None) -> None:
        """在 except 块内使用：error 级别并附带当前异常堆栈（对齐 logging.Logger.exception 语义）。"""
        tb = traceback.format_exc()
        payload = {"traceback": tb}
        if detail is not None:
            if isinstance(detail, dict):
                payload = {**detail, "traceback": tb}
            else:
                payload = {"detail": detail, "traceback": tb}
        _write(self._module, "error", event, payload)


def get_logger(module: str) -> Logger:
    return Logger(module)
