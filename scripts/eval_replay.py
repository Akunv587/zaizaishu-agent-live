"""重放评估脚本（PROMPTS 步骤 3.4；PRD §6 评估体系、DESIGN §10 D3）。

评估模式：python scripts/eval_replay.py --file replay/sample-001.jsonl [--limit 30]
  经 main.run_main_loop 的 reply_wrapper 捕获 chat 的「弹幕原文+生成话术」配对
  （链路与线上同一条代码路径，不复制业务逻辑），产出 evaluation-YYYYMMDD.md，
  每行三个空白评分列（相关/自然/不越界各 0~2），末尾留汇总区占位。

汇总模式：python scripts/eval_replay.py --summary evaluation-20260831.md
  解析人工打分，输出三列平均分与相关率（相关 ≥1 视为相关，目标 95%），
  结果写回文件汇总区。
"""
import argparse
import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import main as main_mod  # noqa: E402  复用主循环装配
from hub.config import ConfigError, load_config  # noqa: E402
from hub.logger import setup as setup_logger  # noqa: E402
from hub.stats import StatsCollector  # noqa: E402

SUMMARY_MARKER = "## 汇总（--summary 生成）"


def pair_from_context(context: dict, reply: str) -> dict:
    """一组配对：弹幕原文（聚合组逐行）与最终播出话术。"""
    members = (context or {}).get("members") or []
    if not members:
        members = [((context or {}).get("user", ""), (context or {}).get("content", ""))]
    return {
        "danmaku": "<br>".join(f"{user}：{content}" for user, content in members),
        "reply": str(reply),
    }


def _cell(text: str) -> str:
    """markdown 表格单元格转义：竖线换成全角，防打断表格。"""
    return str(text).replace("|", "｜").replace("\n", "<br>")


def render_evaluation_md(pairs: list, meta: dict) -> str:
    lines = [
        f"# 提示词评估 · {meta.get('date')}",
        "",
        f"- 重放文件: {meta.get('file')}",
        f"- 配对样本: {len(pairs)}",
        f"- 生成模型: {meta.get('model')}",
        f"- 生成失败兜底: {meta.get('fallback_count', 0)} 条",
        "",
        "| 序号 | 观众弹幕 | AI 话术 | 相关(0~2) | 自然(0~2) | 不越界(0~2) |",
        "|---|---|---|---|---|---|",
    ]
    for i, p in enumerate(pairs, 1):
        lines.append(f"| {i} | {_cell(p['danmaku'])} | {_cell(p['reply'])} |  |  |  |")
    lines += [
        "",
        SUMMARY_MARKER,
        "",
        "（人工打分完成后运行 "
        "`python scripts\\eval_replay.py --summary 本文件路径` 生成汇总；"
        "相关率低于 95% 时回到步骤 3.2 调整 persona.style 或 game 知识库后重评）",
        "",
    ]
    return "\n".join(lines)


def _score(cell: str):
    """单元格转分数：0~2 的整数才有效，空白/非法返回 None。"""
    cell = (cell or "").strip()
    if re.fullmatch(r"[0-2]", cell):
        return int(cell)
    return None


def parse_scored_rows(text: str) -> list:
    """解析评估文件表格：返回 [{danmaku, reply, scores(三元组或None)}]。"""
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 6 or cells[0] in ("序号", "---", ":---") or set(cells[0]) <= {"-", ":"}:
            continue
        danmaku, reply = cells[1], cells[2]
        scores = tuple(_score(c) for c in cells[3:6])
        rows.append({"danmaku": danmaku, "reply": reply, "scores": scores})
    return rows


def summarize_rows(rows: list) -> dict:
    """三列平均分与相关率（相关 ≥1 视为相关；目标 95%）。"""
    scored = [r for r in rows if all(s is not None for s in r["scores"])]
    if not scored:
        return {"scored": 0, "total": len(rows), "pass": None}
    rel = [r["scores"][0] for r in scored]
    nat = [r["scores"][1] for r in scored]
    bound = [r["scores"][2] for r in scored]
    rate = sum(1 for s in rel if s >= 1) / len(scored)
    return {
        "scored": len(scored),
        "total": len(rows),
        "rel_avg": round(sum(rel) / len(scored), 2),
        "nat_avg": round(sum(nat) / len(scored), 2),
        "bound_avg": round(sum(bound) / len(scored), 2),
        "relevance_rate": round(rate * 100, 1),
        "pass": rate >= 0.95,
    }


def write_summary(path: Path, result: dict) -> str:
    """把汇总写回文件（已有汇总区则替换，不重复追加）。"""
    text = path.read_text(encoding="utf-8")
    if SUMMARY_MARKER in text:
        text = text.split(SUMMARY_MARKER)[0].rstrip() + "\n\n"
    verdict = "达标" if result.get("pass") else "未达标"
    section = [
        SUMMARY_MARKER,
        "",
        f"- 汇总时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 打分样本: {result['scored']} / {result['total']} 行",
        f"- 相关平均: {result.get('rel_avg')} / 2",
        f"- 自然平均: {result.get('nat_avg')} / 2",
        f"- 不越界平均: {result.get('bound_avg')} / 2",
        f"- 相关率: {result.get('relevance_rate')}%（目标 ≥95%）—— {verdict}",
        "",
        "（相关率低于 95% 时回到步骤 3.2 调整 persona.style 或 game 知识库后重评，"
        "迭代过程追加记录在本文件末尾）",
        "",
    ]
    text += "\n".join(section)
    path.write_text(text, encoding="utf-8")
    return text


def run_eval(args) -> int:
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"[错误] 配置加载失败：\n{e}")
        return 1

    llm = cfg.get("llm", {}) or {}
    key = str(llm.get("api_key", "")).strip()
    if not key or key.upper().startswith("PLACEHOLDER"):
        print("[错误] llm.api_key 未填真实密钥——评估针对真实 LLM 话术，请先在 config.yaml 填入")
        return 1
    if not Path(args.file).exists():
        print(f"[错误] 重放文件不存在: {args.file}")
        return 1

    setup_logger(Path("logs"), (cfg.get("app", {}) or {}).get("log_level", "info"))
    stats = StatsCollector()
    pairs: list = []
    stop = asyncio.Event()
    limit = max(int(args.limit), 1)

    def wrap(builder):
        async def wrapped(context):
            text = await builder(context)
            pairs.append(pair_from_context(context, text))
            if len(pairs) >= limit:
                stop.set()
            return text
        return wrapped

    print(f"评估重放：{args.file}（speed={args.speed}，limit={limit}）")
    try:
        asyncio.run(main_mod.run_main_loop(
            cfg, stats, stop=stop, replay_file=args.file,
            speed=args.speed, reply_wrapper=wrap))
    except KeyboardInterrupt:
        pass

    if not pairs:
        print("[错误] 未捕获到任何 chat 配对——重放文件里可能没有可聚合的弹幕")
        return 1

    fallback_count = sum(1 for p in pairs if p["reply"] == str((cfg.get("safety", {}) or {}).get("fallback_reply", "")))
    out = Path(f"evaluation-{datetime.now().strftime('%Y%m%d')}.md")
    md = render_evaluation_md(pairs, {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "file": args.file,
        "model": llm.get("model"),
        "fallback_count": fallback_count,
    })
    out.write_text(md, encoding="utf-8")

    print(f"评估文件已生成: {out}（配对 {len(pairs)} 条，其中兜底话术 {fallback_count} 条）")
    print("下一步：人工填写三列评分（0~2），完成后运行 "
          f"python scripts\\eval_replay.py --summary {out}")
    return 0


def run_summary(args) -> int:
    path = Path(args.summary)
    if not path.exists():
        print(f"[错误] 评估文件不存在: {path}")
        return 1
    rows = parse_scored_rows(path.read_text(encoding="utf-8"))
    result = summarize_rows(rows)
    if not result["scored"]:
        print(f"[提示] 没有已打分的行（共 {result['total']} 行）——请先人工填写评分列")
        return 1

    write_summary(path, result)
    print(f"打分样本: {result['scored']} / {result['total']} 行")
    print(f"相关平均: {result['rel_avg']} / 2   自然平均: {result['nat_avg']} / 2   "
          f"不越界平均: {result['bound_avg']} / 2")
    verdict = "达标" if result["pass"] else "未达标（回到步骤 3.2 调整后重评）"
    print(f"相关率: {result['relevance_rate']}%（目标 ≥95%）—— {verdict}")
    print(f"汇总已写回: {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="提示词评估：重放采样打分与汇总")
    parser.add_argument("--file", help="评估模式：重放的 JSONL 文件")
    parser.add_argument("--limit", type=int, default=30, help="最多采样的配对数（默认 30）")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--speed", type=float, default=1.0, help="重放倍速（默认 1.0）")
    parser.add_argument("--summary", metavar="FILE", help="汇总模式：已人工打分的评估文件")
    args = parser.parse_args()

    if args.summary:
        return run_summary(args)
    if args.file:
        return run_eval(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
