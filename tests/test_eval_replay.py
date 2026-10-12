"""评估脚本的单元测试（PROMPTS 步骤 3.4）。

只测纯函数：配对构造、markdown 渲染、打分解析、汇总计算、写回不重复。
重放+真实密钥的端到端评估由人工验收（步骤 3.4 验收标准）。
"""
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import eval_replay  # noqa: E402


def test_pair_from_context_merged_group():
    ctx = {"user": "甲", "content": "这是什么游戏", "members": [("甲", "这是什么游戏"), ("乙", "这是什么游戏呀")]}
    pair = eval_replay.pair_from_context(ctx, "这是超好玩的游戏哈")
    assert pair["danmaku"] == "甲：这是什么游戏<br>乙：这是什么游戏呀"
    assert pair["reply"] == "这是超好玩的游戏哈"


def test_pair_from_context_single_member_fallback():
    pair = eval_replay.pair_from_context({"user": "甲", "content": "单条"}, "回复")
    assert pair["danmaku"] == "甲：单条"


def test_render_evaluation_md_structure():
    pairs = [
        {"danmaku": "甲：这是什么游戏", "reply": "这是超好玩的游戏哈"},
        {"danmaku": "乙：好玩吗", "reply": "好玩哈兄弟"},
    ]
    md = eval_replay.render_evaluation_md(pairs, {"date": "2026-08-31", "file": "r.jsonl",
                                                  "model": "test-model", "fallback_count": 1})
    assert "提示词评估 · 2026-08-31" in md
    assert "重放文件: r.jsonl" in md
    assert "生成模型: test-model" in md
    assert "配对样本: 2" in md
    assert "| 1 | 甲：这是什么游戏 | 这是超好玩的游戏哈 |  |  |  |" in md
    assert "相关(0~2)" in md and "自然(0~2)" in md and "不越界(0~2)" in md
    assert eval_replay.SUMMARY_MARKER in md


def test_render_escapes_pipe_and_newline():
    pairs = [{"danmaku": "甲：666", "reply": "哈哈|兄弟们\n冲"}]
    md = eval_replay.render_evaluation_md(pairs, {"date": "x", "file": "f", "model": "m"})
    assert "| 1 | 甲：666 | 哈哈｜兄弟们<br>冲 |" in md


def test_parse_scored_rows_skips_header_and_separator():
    md = eval_replay.render_evaluation_md(
        [{"danmaku": "甲：问", "reply": "答"}], {"date": "d", "file": "f", "model": "m"})
    rows = eval_replay.parse_scored_rows(md)
    assert len(rows) == 1
    assert rows[0]["danmaku"] == "甲：问"
    assert rows[0]["scores"] == (None, None, None)


def test_parse_scored_rows_reads_scores():
    text = "| 序号 | 观众弹幕 | AI 话术 | 相关(0~2) | 自然(0~2) | 不越界(0~2) |\n" \
           "|---|---|---|---|---|---|\n" \
           "| 1 | 甲：问 | 答 | 2 | 1 | 0 |\n" \
           "| 2 | 乙：问 | 答2 |  | 1 | 2 |\n" \
           "| 3 | 丙：问 | 答3 | 3 | 1 | 1 |\n"
    rows = eval_replay.parse_scored_rows(text)
    assert len(rows) == 3
    assert rows[0]["scores"] == (2, 1, 0)
    assert rows[1]["scores"][0] is None
    assert rows[2]["scores"][0] is None  # 3 超出 0~2 视为未打分


def test_summarize_rows_computes_rate():
    rows = [
        {"scores": (2, 2, 2)},
        {"scores": (1, 1, 1)},
        {"scores": (0, 2, 2)},
        {"scores": (None, 1, 1)},
    ]
    result = eval_replay.summarize_rows(rows)
    assert result["scored"] == 3
    assert result["rel_avg"] == 1.0
    assert result["relevance_rate"] == 66.7
    assert result["pass"] is False


def test_summarize_rows_pass_threshold():
    rows = [{"scores": (2, 2, 2)}] * 19 + [{"scores": (0, 2, 2)}]
    result = eval_replay.summarize_rows(rows)
    assert result["relevance_rate"] == 95.0
    assert result["pass"] is True


def test_summarize_rows_no_scores():
    assert eval_replay.summarize_rows([{"scores": (None, None, None)}])["pass"] is None


def test_write_summary_creates_then_replaces(tmp_path):
    md = eval_replay.render_evaluation_md(
        [{"danmaku": "甲：问", "reply": "答"}], {"date": "d", "file": "f", "model": "m"})
    path = tmp_path / "evaluation-test.md"
    path.write_text(md, encoding="utf-8")

    # 人工打分：把第一行的三个空格列填上 2/1/2
    scored = md.replace("| 1 | 甲：问 | 答 |  |  |  |", "| 1 | 甲：问 | 答 | 2 | 1 | 2 |")
    path.write_text(scored, encoding="utf-8")

    rows = eval_replay.parse_scored_rows(path.read_text(encoding="utf-8"))
    result = eval_replay.summarize_rows(rows)
    eval_replay.write_summary(path, result)

    text = path.read_text(encoding="utf-8")
    assert text.count(eval_replay.SUMMARY_MARKER) == 1  # 占位区被替换，不叠加
    assert "相关率: 100.0%（目标 ≥95%）—— 达标" in text

    # 再次写回应替换而非追加
    eval_replay.write_summary(path, result)
    text2 = path.read_text(encoding="utf-8")
    assert text2.count("汇总时间:") == 1
