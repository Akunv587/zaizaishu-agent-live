"""当日运行报告（PROMPTS 步骤 6.2；PRD §2 指标、OQ-4 成本核算）。

读取 logs\\live-YYYYMMDD.jsonl 结构化日志（JSON 行），汇总当日全部场次：
- 计数类：弹幕事件、任务产出、播放、作废、拦截、LLM/TTS 失败（口径=各场次
  shutdown 记录之和；当日最后一场若未正常退出会漏，报告标注）
- 延迟：按场次采样数加权的平均端到端延迟、全场最大值
- 字符消耗：LLM（请求 prompt_chars + 响应 reply_chars，来自
  llm_reply_generated / llm_request_failed 留痕）与 TTS（tts_stream_done 的
  chars）——只统计成功请求的响应字符与发出请求的输入字符
- 成本估算：字符量 × 头部单价常量（假设单价，主播按实际账单更新后复核 OQ-4）

用法：python scripts/report.py [YYYYMMDD]（默认今天）
输出：report-YYYYMMDD.md（仓库根目录，重复运行覆盖）
"""
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"

# ── 成本单价假设（OQ-4：占位标价快照，来源与日期注明，主播按实际账单更新）──
# LLM：OpenAI 兼容平台 gpt-4o-mini 量级输入价折算（$0.15/1M tokens ≈ ¥1.1/1M，
# 中文字符≈1 token 口径），2026-09-01 快照估算
LLM_CNY_PER_KCHARS = 0.002
# TTS：MiniMax speech-2.8-hd 量级（¥200/百万字符 量级），2026-09-01 快照估算
TTS_CNY_PER_KCHARS = 0.2

SUM_FIELDS = (
    "danmaku_in", "task_produced", "played", "dropped_stale", "dropped_pause",
    "blocked_input", "blocked_output", "llm_fail", "tts_fail",
    "idle_announced", "silent_fallback",
)


def load_day(date_s: str) -> list:
    path = LOGS_DIR / f"live-{date_s}.log"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def summarize(rows: list, date_s: str) -> dict:
    s = {name: 0 for name in SUM_FIELDS}
    s["_llm_prompt_chars"] = 0
    s["_llm_reply_chars"] = 0
    s["_tts_chars"] = 0
    sessions = 0
    latency_sum = 0.0
    latency_n = 0
    latency_max = 0.0
    for r in rows:
        ev, detail = r.get("event"), r.get("detail") or {}
        if ev == "shutdown" and isinstance(detail, dict):
            sessions += 1
            for name in SUM_FIELDS:
                s[name] += int(detail.get(name, 0) or 0)
            n = int(detail.get("latency_samples", 0) or 0)
            if n:
                latency_n += n
                latency_sum += float(detail.get("latency_avg_s") or 0) * n
                latency_max = max(latency_max, float(detail.get("latency_max_s") or 0))
        elif ev == "llm_reply_generated" and isinstance(detail, dict):
            s["_llm_prompt_chars"] += int(detail.get("prompt_chars", 0) or 0)
            s["_llm_reply_chars"] += int(detail.get("reply_chars", 0) or 0)
        elif ev == "llm_request_failed" and isinstance(detail, dict):
            s["_llm_prompt_chars"] += int(detail.get("prompt_chars", 0) or 0)
        elif ev == "tts_stream_done" and isinstance(detail, dict):
            s["_tts_chars"] += int(detail.get("chars", 0) or 0)
    s["sessions"] = sessions
    s["latency_avg_s"] = round(latency_sum / latency_n, 2) if latency_n else None
    s["latency_max_s"] = round(latency_max, 2) if latency_max else None
    s["date"] = date_s
    return s


def render(s: dict) -> str:
    llm_chars = s["_llm_prompt_chars"] + s["_llm_reply_chars"]
    tts_chars = s["_tts_chars"]
    llm_cost = llm_chars * LLM_CNY_PER_KCHARS / 1000
    tts_cost = tts_chars * TTS_CNY_PER_KCHARS / 1000
    total = llm_cost + tts_cost
    reply_ratio = (s["task_produced"] / s["danmaku_in"] * 100) if s["danmaku_in"] else None
    latency = f"{s['latency_avg_s']}s / 最大 {s['latency_max_s']}s" \
        if s["latency_avg_s"] is not None else "暂无采样"

    lines = [
        f"# 仔仔鼠运行日报 {s['date']}",
        "",
        f"- 场次（正常退出的 shutdown 记录）：{s['sessions']}",
        f"- 弹幕事件：{s['danmaku_in']}（含五类事件）",
        f"- 任务产出：{s['task_produced']}（其中口播 {s['idle_announced']}、静默兜底 {s['silent_fallback']}）",
        f"- 播放：{s['played']}   作废：过发 {s['dropped_stale']} + 暂停清理 {s['dropped_pause']}",
        f"- 任务/弹幕比：{f'{reply_ratio:.0f}%' if reply_ratio is not None else '—'}"
        "（口径参考：有问必答响应率的粗替代，聚合合并会压低该值，精确响应率见 eval_replay）",
        f"- 拦截：输入 {s['blocked_input']}、输出 {s['blocked_output']}",
        f"- 失败：LLM {s['llm_fail']}、TTS {s['tts_fail']}",
        f"- 端到端延迟（加权平均 / 最大）：{latency}",
        "",
        "## 字符消耗与成本估算",
        "",
        f"- LLM：请求+响应共 {llm_chars} 字符"
        f"（输入 {s['_llm_prompt_chars']} / 输出 {s['_llm_reply_chars']}）",
        f"- TTS：合成 {tts_chars} 字符",
        "",
        "| 项 | 消耗 | 单价假设 | 估算成本 |",
        "|---|---|---|---|",
        f"| LLM | {llm_chars} 字符 | ¥{LLM_CNY_PER_KCHARS}/千字符 | ¥{llm_cost:.2f} |",
        f"| TTS | {tts_chars} 字符 | ¥{TTS_CNY_PER_KCHARS}/千字符 | ¥{tts_cost:.2f} |",
        f"| **合计** | — | — | **¥{total:.2f}** |",
        "",
        f"- 对照 PRD §2 目标（单日 ≤ 20 元）：{'达标' if total <= 20 else '超出'}"
        + (f"，最大消耗项：{'TTS' if tts_cost > llm_cost else 'LLM'}" if total > 20 else ""),
        "- 单价为占位假设（OQ-4），来源与日期见 scripts/report.py 头部注释；"
        "请按平台实际账单更新常量后复核。",
        "",
        "> 口径说明：计数来自各场次 shutdown 汇总；当日最后一场若未正常退出则不计入。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    date_s = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y%m%d")
    rows = load_day(date_s)
    if not rows:
        print(f"[错误] 当日无日志：logs/live-{date_s}.log 不存在或为空")
        return 1
    s = summarize(rows, date_s)
    out = ROOT / f"report-{date_s}.md"
    out.write_text(render(s), encoding="utf-8")
    print(render(s))
    print(f"已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
