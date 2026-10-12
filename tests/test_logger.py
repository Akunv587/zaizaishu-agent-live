"""hub.logger 的单元测试（PROMPTS 步骤 0.2）。"""
import json
from datetime import datetime
from pathlib import Path

from hub import logger as logger_mod


def test_json_lines_and_fields(tmp_path, monkeypatch, capsys):
    logger_mod.setup(tmp_path, "debug")
    log = logger_mod.get_logger("test_module")
    log.info("event_a", {"user": "张三", "text": "主播好"})
    log.warning("event_b", "简单字符串")
    log.debug("event_c", {"n": 1})

    files = list(tmp_path.glob("live-*.log"))
    assert len(files) == 1
    day = datetime.now().strftime("%Y%m%d")
    assert files[0].name == f"live-{day}.log"

    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    # info 与 debug 都应写入（级别 debug）；warning 在 console 不应输出 debug 行
    records = [json.loads(line) for line in lines]
    events = [r["event"] for r in records]
    assert events == ["event_a", "event_b", "event_c"]
    for r in records:
        assert set(r.keys()) >= {"ts", "module", "level", "event", "detail"}
        assert r["module"] == "test_module"
    assert records[0]["detail"] == {"user": "张三", "text": "主播好"}
    assert records[1]["detail"] == "简单字符串"

    console = capsys.readouterr().out
    assert "event_a" in console
    assert "test_module" in console


def test_level_filter(tmp_path):
    logger_mod.setup(tmp_path, "warning")
    log = logger_mod.get_logger("m")
    log.info("should_not_write")
    log.error("should_write")
    content = (tmp_path / f"live-{datetime.now().strftime('%Y%m%d')}.log").read_text(encoding="utf-8")
    assert "should_not_write" not in content
    assert "should_write" in content


def test_daily_rollover(tmp_path, monkeypatch):
    logger_mod.setup(tmp_path, "info")
    frozen = {"now": datetime(2026, 8, 31, 23, 59, 59)}

    class FakeDT(datetime):
        @classmethod
        def now(cls):
            return frozen["now"]

    monkeypatch.setattr(logger_mod, "_now", lambda: frozen["now"])
    log = logger_mod.get_logger("m")
    log.info("day1_event")

    frozen["now"] = datetime(2026, 9, 1, 0, 0, 1)
    log.info("day2_event")

    day1 = tmp_path / "live-20260831.log"
    day2 = tmp_path / "live-20260901.log"
    assert "day1_event" in day1.read_text(encoding="utf-8")
    assert "day2_event" in day2.read_text(encoding="utf-8")
