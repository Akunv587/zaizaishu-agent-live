# -*- coding: utf-8 -*-
"""ui_audit.py —— 前端配色与变量审计（构建期/验收用，不参与运行时）。

做三件事：
  ① CSS 变量完整性：定义 / 引用 / 引用未定义 / 自引用
  ② 对比度：按「实际用法」用 WCAG 2.1 公式实测关键「底/字」组合
  ③ 蓝紫残留：按 HSL 色相扫描字面量色值与内联 SVG 色值

用法：
    python scripts/ui_audit.py            # 只看结论
    python scripts/ui_audit.py -v         # 打印逐项明细

新增页面或改配色后必须重跑；有 FAIL 就不算做完（`--allow-fail` 仅在知情决策下使用）。
"""
from __future__ import annotations

import argparse
import colorsys
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "webui" / "index.html"

# 蓝紫判定区间：色相 195~300 且饱和 > 15% 且明度 > 50%
BLUE_HUE = (195, 300)


def srgb_channel(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * srgb_channel(r) + 0.7152 * srgb_channel(g) + 0.0722 * srgb_channel(b)


def contrast(fg: str, bgc: str) -> float:
    l1, l2 = sorted((luminance(fg), luminance(bgc)), reverse=True)
    return (l1 + 0.05) / (l2 + 0.05)


def mix(a: str, b: str, t: float) -> str:
    ha, hb = a.lstrip("#"), b.lstrip("#")
    ra, rb = (int(ha[i:i + 2], 16) for i in (0, 2, 4)), (int(hb[i:i + 2], 16) for i in (0, 2, 4))
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ra, rb))


def parse_root(src: str) -> dict[str, str]:
    block = src.split(":root {", 1)[1].split("\n  }", 1)[0]
    return dict(re.findall(r'(--[a-zA-Z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})\s*;', block))


def check_vars(src: str, root: dict[str, str], verbose: bool) -> list[str]:
    block = src.split(":root {", 1)[1].split("\n  }", 1)[0]
    defined = set(re.findall(r'^\s*(--[a-zA-Z0-9-]+)\s*:', block, re.M))
    nocomment = re.sub(r'/\*.*?\*/', "", src, flags=re.S)
    used = set(re.findall(r'var\(\s*(--[a-zA-Z0-9-]+)', nocomment))
    undef = sorted(used - defined)
    selfref = sorted(
        v for v in defined
        if re.search(r'%s\s*:\s*var\(%s\)' % (re.escape(v), re.escape(v)), block)
    )
    unused = sorted(defined - used)

    print("=== ① CSS 变量完整性 ===")
    print("  定义 %d / 引用 %d / 未定义 %d / 自引用 %d" % (
        len(defined), len(used), len(undef), len(selfref)))
    problems = []
    for u in undef:
        print("  !! 引用了未定义的变量：%s" % u)
        problems.append("变量未定义 %s" % u)
    for v in selfref:
        print("  !! 自引用（整条会失效且不报错）：%s" % v)
        problems.append("变量自引用 %s" % v)
    if verbose and unused:
        print("  · 定义未引用（多半无害）：%s" % ", ".join(unused))
    return problems


def build_rows(v: dict) -> list[tuple[str, str, str, float]]:
    """返回 (说明, 前景, 底, 门槛)。空前景表示分组标题。"""
    W = "#ffffff"
    prim, solid = v.get("--brand-primary", "#000"), v.get("--brand-solid", "#000")
    press, deep = v.get("--brand-press", "#000"), v.get("--brand-deep", "#000")
    bg, surf = v.get("--bg", "#fff"), v.get("--surface", "#fff")
    return [
        ("── 基础文字 ──", "", "", 0),
        ("正文 / 页面底", v.get("--ink", ""), bg, 4.5),
        ("正文 / 卡片底", v.get("--ink", ""), surf, 4.5),
        ("次要字 / 页面底", v.get("--ink-2", ""), bg, 4.5),
        ("次要字 / 卡片底", v.get("--ink-2", ""), surf, 4.5),
        ("弱提示字 / 页面底", v.get("--ink-3", ""), bg, 3.0),
        ("禁用字 / 卡片底", v.get("--ink-4", ""), surf, 1.0),
        ("── 次级卡片底（surface-2：试玩结果 / 聚合卡 / 决策列表）──", "", "", 0),
        ("正文 / surface-2", v.get("--ink", ""), v.get("--surface-2", ""), 4.5),
        ("次要字 / surface-2", v.get("--ink-2", ""), v.get("--surface-2", ""), 4.5),
        ("弱提示字 / surface-2", v.get("--ink-3", ""), v.get("--surface-2", ""), 3.0),
        ("── 品牌色当文字 ──", "", "", 0),
        ("品牌深字 / 卡片底", deep, surf, 4.5),
        ("品牌深字 / subtle 底", deep, v.get("--brand-subtle", ""), 4.5),
        ("品牌深字 / soft 底", deep, v.get("--brand-soft", ""), 4.5),
        ("品牌深字 / mist 底", deep, v.get("--brand-mist", ""), 4.5),
        ("── 实心按钮（白字）──", "", "", 0),
        ("白字 / 渐变·亮端", W, solid, 3.0),
        ("白字 / 渐变·中点", W, mix(solid, press, 0.5), 3.0),
        ("白字 / 渐变·暗端", W, press, 3.0),
        ("白字 / 深绿按钮", W, deep, 4.5),
        ("白字 / 品牌主色（应作纯装饰）", W, prim, 1.0),
        ("── 品牌色当图形 ──", "", "", 0),
        ("图标描边 / 卡片底", press, surf, 3.0),
        ("状态描边 / 卡片底", press, surf, 3.0),
        ("── 语义文字 ──", "", "", 0),
        ("成功字 / 卡片底", v.get("--green-text", ""), surf, 4.5),
        ("成功字 / green-soft 底", v.get("--green-text", ""), v.get("--green-soft", ""), 4.5),
        ("错误字 / 卡片底", v.get("--red-text", ""), surf, 4.5),
        ("错误字 / red-soft 底", v.get("--red-text", ""), v.get("--red-soft", ""), 4.5),
        ("警示字 / 卡片底", v.get("--gold-text", ""), surf, 4.5),
        ("警示字 / gold-soft 底", v.get("--gold-text", ""), v.get("--gold-soft", ""), 4.5),
        ("白勾 / 成功圆底", W, v.get("--green", ""), 3.0),
        ("白叉 / 失败圆底", W, v.get("--red", ""), 3.0),
        ("── M6 反馈采集：消息流三个图标按钮 ──", "", "", 0),
        ("反馈图标 / 按钮底", v.get("--ink-2", ""), surf, 3.0),
        ("反馈图标·悬停 / soft 底", deep, v.get("--brand-soft", ""), 3.0),
        ("── M6 反馈采集：结果标记字（.fb-mark）──", "", "", 0),
        ("采纳标记字 / surface-2", v.get("--green-text", ""), v.get("--surface-2", ""), 4.5),
        ("弃用标记字 / surface-2", v.get("--red-text", ""), v.get("--surface-2", ""), 4.5),
        ("改写标记字 / surface-2", v.get("--gold-text", ""), v.get("--surface-2", ""), 4.5),
        ("── M6 对话调参：发送键与会话气泡 ──", "", "", 0),
        # 与全站实心按钮同一口径：渐变暗端当底、白字，门槛 3.0
        # （本键取的就是暗端 #009273，比全站主按钮的亮端更稳）
        ("白字 / 调参发送键", W, press, 3.0),
        ("气泡字·我方 / brand-soft 底", deep, v.get("--brand-soft", ""), 4.5),
        ("气泡字·助手 / surface-2", v.get("--ink", ""), v.get("--surface-2", ""), 4.5),
        ("气泡字·失败 / red-soft 底", v.get("--red-text", ""), v.get("--red-soft", ""), 4.5),
        ("快照行字 / 卡片底", v.get("--ink-2", ""), surf, 4.5),
        ("── M6 反馈进化：条形段与图例色块（1.4.11 需 ≥3:1，故用深档）──", "", "", 0),
        ("图例色块·不像主播 / surface-2", press, v.get("--surface-2", ""), 3.0),
        ("图例色块·太长 / surface-2", v.get("--gold-text", ""), v.get("--surface-2", ""), 3.0),
        ("图例色块·跑题 / surface-2", v.get("--red-text", ""), v.get("--surface-2", ""), 3.0),
        ("图例色块·重复 / surface-2", v.get("--ink-2", ""), v.get("--surface-2", ""), 3.0),
        ("条形段·不像主播 / 轨道底", press, v.get("--surface-3", ""), 3.0),
        ("条形段·太长 / 轨道底", v.get("--gold-text", ""), v.get("--surface-3", ""), 3.0),
        ("条形段·跑题 / 轨道底", v.get("--red-text", ""), v.get("--surface-3", ""), 3.0),
        ("条形段·重复 / 轨道底", v.get("--ink-2", ""), v.get("--surface-3", ""), 3.0),
    ]


def check_contrast(v: dict) -> list[str]:
    print()
    print("=== ② 对比度（按实际用法实测，非目视）===")
    print("  %-30s %-9s %-9s %8s %6s %s" % ("组合", "前景", "底", "比值", "门槛", "判定"))
    fails = []
    for label, fg, bgc, need in build_rows(v):
        if not fg:
            print("  %s" % label)
            continue
        if not fg.startswith("#") or not bgc.startswith("#"):
            print("  %-30s 变量缺失，跳过" % label)
            continue
        r = contrast(fg, bgc)
        ok = r >= need
        if not ok:
            fails.append("%s %.2f:1（需 %.1f）" % (label, r, need))
        print("  %-30s %-9s %-9s %7.2f:1 %5.1f %s" % (label, fg, bgc, r, need, "PASS" if ok else "FAIL"))
    return fails


def check_bluepurple(src: str, verbose: bool) -> list[str]:
    nums = re.findall(r'#[0-9a-fA-F]{6}\b', src)
    cnt = Counter(nums)
    print()
    print("=== ③ 蓝紫残留（色相 %d~%d° / 饱和>15%% / 明度>50%%）===" % BLUE_HUE)
    print("  字面量 hex 共 %d 种 / %d 次" % (len(cnt), sum(cnt.values())))
    hits = []
    for c, n in sorted(cnt.items()):
        r, g, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
        h, s, val = colorsys.rgb_to_hsv(r, g, b)
        hue = h * 360
        if BLUE_HUE[0] <= hue <= BLUE_HUE[1] and s > 0.15 and val > 0.5:
            hits.append("%s x%d (H=%d° S=%d%%)" % (c, n, round(hue), round(s * 100)))
    if hits:
        print("  !! 疑似蓝紫残留：")
        for x in hits:
            print("     %s" % x)
    else:
        print("  无蓝紫残留 ✓")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="前端配色与变量审计")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印明细")
    ap.add_argument("--allow-fail", action="store_true", help="有 FAIL 也返回 0")
    args = ap.parse_args()

    if not INDEX.exists():
        print("找不到 %s" % INDEX, file=sys.stderr)
        return 2
    src = INDEX.read_text(encoding="utf-8")
    root = parse_root(src)

    problems = check_vars(src, root, args.verbose)
    problems += check_contrast(root)
    problems += check_bluepurple(src, args.verbose)

    print()
    print("=== 结论 ===")
    if problems:
        print("  未通过 %d 项：" % len(problems))
        for p in problems:
            print("    - %s" % p)
        return 0 if args.allow_fail else 1
    print("  全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
