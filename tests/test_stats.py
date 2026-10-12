"""hub.stats 的单元测试（PROMPTS 步骤 0.2）。"""
from hub.stats import StatsCollector


def test_counters_and_summary():
    stats = StatsCollector()
    stats.inc("danmaku_in", 3)
    stats.inc("played")
    stats.inc("llm_fail", 2)
    s = stats.summary()
    assert s["danmaku_in"] == 3
    assert s["played"] == 1
    assert s["llm_fail"] == 2
    assert s["tts_fail"] == 0
    assert s["latency_avg_s"] is None


def test_latency_stats():
    stats = StatsCollector()
    for v in [1.0, 2.0, 6.0]:
        stats.record_latency(v)
    s = stats.summary()
    assert s["latency_avg_s"] == 3.0
    assert s["latency_max_s"] == 6.0
    assert s["latency_samples"] == 3


def test_format_summary_contains_all_counters():
    stats = StatsCollector()
    stats.inc("dropped_stale", 5)
    text = stats.format_summary()
    for keyword in ["弹幕事件", "生成回复", "播出", "过发作废", "输入拦截", "输出拦截", "LLM失败", "TTS失败", "端到端延迟"]:
        assert keyword in text
    assert "5" in text


def test_reset_returns_old_value_and_zeroes():
    stats = StatsCollector()
    stats.inc("llm_fail", 10)
    stats.inc("idle_announced")  # extra 计数器同样可复位
    assert stats.reset("llm_fail") == 10
    assert stats.reset("idle_announced") == 1
    assert stats.get("llm_fail") == 0
    assert stats.get("idle_announced") == 0
    assert stats.reset("llm_fail") == 0  # 已清零再复位返回 0
    assert stats.reset("unknown_counter") == 0  # 未知计数器不报错
