# -*- coding: utf-8 -*-
"""emoji_scan.py —— 前端 emoji / 装饰字符扫描（构建期验收用）。

用 Unicode 区间判定，比 grep 字面量可靠（emoji 通常带变体选择符 U+FE0F，肉眼难检索）。
默认扫描 webui/ 下的 html/js/css 与仓库根的 desktop.py。

判定区间内的字符都会报出码点（U+XXXX），便于判断是「真 emoji」还是「正常标点」：
  → (U+2192) 属排版标点，不算 emoji，正文与注释里保留是合理的；
  ✓ ✗ ⚠ 💡 ✏️ 等属字符图标，前端应改为 SVG。

用法：
    python scripts/emoji_scan.py            # 只报 emoji 类
    python scripts/emoji_scan.py --all      # 连排版标点一起报
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 真 emoji / 符号图标区间
EMOJI_RANGES = [
    (0x1F300, 0x1FAFF),   # 杂项符号与象形
    (0x1F000, 0x1F2FF),   # 麻将/扑克/封闭补充
    (0x1F1E6, 0x1F1FF),   # 区域指示符（国旗）
    (0x2600, 0x27BF),     # 杂项符号 + 装饰符号（✓✗⚠✨❤）
    (0x2B00, 0x2BFF),     # 杂项符号与箭头
    (0xFE0F, 0xFE0F),     # 变体选择符-16
    (0x2700, 0x27BF),     # 装饰符号
]
# 正常排版标点：算「符号」但不算 emoji
TYPO_RANGES = [
    (0x2190, 0x21FF),     # 箭头 → ←
    (0x2000, 0x206F),     # 通用标点
]


def in_ranges(ch: str, ranges: list[tuple[int, int]]) -> bool:
    o = ord(ch)
    if ch in ("\u200b", "\ufeff", "\u00ad"):   # 零宽/软连字符不算
        return False
    return any(a <= o <= b for a, b in ranges)


def scan(path: Path, include_typo: bool) -> list[tuple[int, str, str, str]]:
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        emo = sorted({c for c in line if in_ranges(c, EMOJI_RANGES)})
        if emo:
            out.append((i, "emoji", "".join(emo), line.strip()[:110]))
        elif include_typo:
            typo = sorted({c for c in line if in_ranges(c, TYPO_RANGES)})
            if typo:
                out.append((i, "标点", "".join(typo), line.strip()[:110]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="前端 emoji / 装饰字符扫描")
    ap.add_argument("--all", action="store_true", help="连排版标点一起报")
    args = ap.parse_args()

    targets = [ROOT / "desktop.py"]
    webui = ROOT / "webui"
    if webui.is_dir():
        targets += sorted(p for p in webui.rglob("*") if p.suffix in (".html", ".js", ".css"))

    emoji_total = typo_total = 0
    for p in targets:
        if not p.exists():
            continue
        hits = scan(p, args.all)
        e = [h for h in hits if h[1] == "emoji"]
        t = [h for h in hits if h[1] == "标点"]
        emoji_total += len(e)
        typo_total += len(t)
        if not hits:
            continue
        rel = p.relative_to(ROOT).as_posix()
        print("### %s  —— emoji %d 行 / 标点 %d 行" % (rel, len(e), len(t)))
        for line_no, kind, chars, ctx in hits:
            cps = " ".join("U+%04X" % ord(c) for c in chars)
            print("  %-5s L%-5d [%s]  %s" % (kind, line_no, cps, ctx))
        print()

    print("合计：emoji %d 行、排版标点 %d 行" % (emoji_total, typo_total))
    if emoji_total:
        print("FAIL —— 前端不允许出现 emoji，请改为 --ico-* 的 SVG mask 图标（见 DESIGN.md §13.3）")
        return 1
    print("PASS —— 无 emoji")
    return 0


if __name__ == "__main__":
    sys.exit(main())
