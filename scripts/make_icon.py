"""生成应用图标 assets/icon.ico（构建期工具，非运行时依赖）。

青绿渐变圆角底 + 白色 Z 字标 + 右上角金色直播信号点，
与控制台页首 logo 完全一致（顶栏 SVG 的 linearGradient #40d1b3 → #009273）。
用法：python scripts/make_icon.py
需要 Pillow（仅构建期，见 requirements-dev.txt）。
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"
SIZE = 512
TOP, BOTTOM = (64, 209, 179), (0, 146, 115)  # #40d1b3 → #009273（品牌青绿）
GOLD = (255, 209, 102, 255)  # #ffd166


def _rounded_gradient(size: int, radius: int) -> Image.Image:
    rows = []
    for y in range(size):
        t = y / (size - 1)
        rows.append([tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM))] * size)
    base = Image.new("RGB", (size, size))
    base.putdata([px for row in rows for px in row])
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(base, (0, 0), mask)
    return out


def _draw_z(img: Image.Image) -> None:
    # 与页面 logo 的 viewBox 40 对齐：M12 13 h16 l-16 14 h16，stroke 3.6
    s = img.width / 40
    pts = [(12 * s, 13 * s), (28 * s, 13 * s), (12 * s, 27 * s), (28 * s, 27 * s)]
    w = round(3.6 * s)
    d = ImageDraw.Draw(img)
    d.line(pts, fill=(255, 255, 255, 255), width=w, joint="curve")
    r = w / 2
    for cx, cy in (pts[0], pts[-1]):  # 圆头端点
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 255))


def _draw_dot(img: Image.Image) -> None:
    s = img.width / 40
    cx, cy, r = 30.5 * s, 9.5 * s, 2.4 * s
    ImageDraw.Draw(img).ellipse([cx - r, cy - r, cx + r, cy + r], fill=GOLD)


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img = _rounded_gradient(SIZE, round(SIZE * 0.25))
    _draw_z(img)
    _draw_dot(img)
    img = img.filter(ImageFilter.SMOOTH_MORE)
    img.save(OUT, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    print(f"icon written: {OUT}")


if __name__ == "__main__":
    main()
