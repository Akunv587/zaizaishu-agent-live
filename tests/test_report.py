"""日报脚本汇总与渲染的单元测试（PROMPTS 步骤 6.2）。

口径：计数=各场次 shutdown 之和；LLM 字符=成功(prompt+reply)与失败(prompt)
留痕累计；TTS 字符=tts_stream_done 留痕累计；延迟=按场次采样数加权。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import report  # noqa: E402


def _row(event, detail=None):
    return {"ts": "2026-09-01T00:00:00.000", "module": "x", "level": "info",
            "event": event, "detail": detail}


def test_summarize_sums_sessions_and_counters():
    rows = [
        _row("echo_event", {"type": "chat"}),
        _row("shutdown", {"danmaku_in": 10, "task_produced": 8, "played": 8,
                          "latency_samples": 4, "latency_avg_s": 3.0, "latency_max_s": 5.0}),
        _row("shutdown", {"danmaku_in": 5, "task_produced": 4, "played": 3,
                          "dropped_stale": 1, "llm_fail": 2,
                          "latency_samples": 1, "latency_avg_s": 6.0, "latency_max_s": 6.0}),
    ]
    s = report.summarize(rows, "20260901")
    assert s["sessions"] == 2
    assert s["danmaku_in"] == 15 and s["task_produced"] == 12 and s["played"] == 11
    assert s["dropped_stale"] == 1 and s["llm_fail"] == 2
    # 加权平均：(3.0*4 + 6.0*1)/5 = 3.6；最大取全场 6.0
    assert s["latency_avg_s"] == 3.6 and s["latency_max_s"] == 6.0


def test_summarize_char_consumption_from_traces():
    rows = [
        _row("llm_reply_generated", {"prompt_chars": 300, "reply_chars": 20}),
        _row("llm_reply_generated", {"prompt_chars": 300, "reply_chars": 18}),
        _row("llm_request_failed", {"prompt_chars": 280}),
        _row("tts_stream_done", {"chars": 20}),
        _row("tts_stream_done", {"chars": 18}),
        _row("shutdown", {"danmaku_in": 2, "task_produced": 2, "played": 2}),
    ]
    s = report.summarize(rows, "20260901")
    assert s["_llm_prompt_chars"] == 300 + 300 + 280
    assert s["_llm_reply_chars"] == 38
    assert s["_tts_chars"] == 38


def test_summarize_missing_fields_defaults_zero():
    s = report.summarize([_row("echo_event", {})], "20260901")
    assert s["sessions"] == 0
    assert s["latency_avg_s"] is None and s["latency_max_s"] is None
    assert s["_llm_prompt_chars"] == 0 and s["_tts_chars"] == 0


def test_render_contains_metrics_and_cost():
    s = report.summarize([], "20260901")
    s.update({"_llm_prompt_chars": 1000, "_llm_reply_chars": 100, "_tts_chars": 200,
              "danmaku_in": 9, "task_produced": 7, "played": 6,
              "idle_announced": 1,
              "dropped_stale": 1, "dropped_pause": 0,
              "blocked_input": 0, "blocked_output": 0,
              "llm_fail": 0, "tts_fail": 0, "sessions": 1,
              "latency_avg_s": 2.5, "latency_max_s": 4.0})
    text = report.render(s)
    for keyword in ("运行日报 20260901", "场次", "弹幕事件：9", "任务产出：7",
                    "LLM：请求+响应共 1100 字符", "TTS：合成 200 字符",
                    "成本", "合计"):
        assert keyword in text, keyword
    # 成本：LLM 1100 字 ×0.002/千 + TTS 200 字 ×0.2/千 = 0.0022+0.04 = ¥0.04
    assert "¥0.04" in text
    assert "达标" in text  # 0.04 ≤ 20 元目标
