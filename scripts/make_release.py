# -*- coding: utf-8 -*-
"""make_release.py —— 组装发布包（M7，见 docs/IMPLEMENTATION.md §四）。

产物布局：

    dist/release/仔仔鼠AI智能体直播助手/
    ├── 仔仔鼠AI智能体直播助手.exe        # 由 dist/zaizaishu-agent.exe 改名而来
    ├── config.yaml                       # 预填双 Key
    ├── blockwords.txt
    ├── 一键安装虚拟声卡.bat
    ├── VBCABLE_Setup_x64.exe
    ├── assets/seed_profile.zip           # **外置种子**（不塞进 exe，见下）
    └── voice_clone/voice_clone_data/     # MiniMax Key（config.json）+ 音色库

**不带**（与旧版一致）：`profiles/`（首跑由外置种子自动落地；用户既有存档不覆盖）。

为什么种子外置：v4 种子（3 情绪档）已到 ~69 MB，内联进 onefile exe 会让 exe 从
~23 MB 膨胀到 ~92 MB，且每次启动都要把它解包到临时目录。外置后 `hub.scriptstore.
find_seed_zip()` 会先找包内、再找 exe 旁 `assets/`，首跑照旧自动落地。

用法：
    python scripts/make_release.py                 # 需要先跑 PyInstaller 出 exe
    python scripts/make_release.py --dry-run
    python scripts/make_release.py --with-uploads   # 连克隆素材 uploads/ 一起带
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "仔仔鼠AI智能体直播助手"
EXE_NAME = f"{APP_NAME}.exe"
BUILT_EXE = ROOT / "dist" / "zaizaishu-agent.exe"
SEED_ZIP = ROOT / "assets" / "seed_profile.zip"
VOICE_DATA = ROOT / "voice_clone" / "voice_clone_data"
EXIT_OK, EXIT_MISSING = 0, 1


def build_release(out_dir: Path = None, *, exe: Path = None, dry_run=False,
                  with_uploads=False, root: Path = ROOT) -> list:
    """组装发布包，返回 [(相对路径, 说明), ...]；缺 exe 时抛 FileNotFoundError。"""
    out = Path(out_dir) if out_dir else root / "dist" / "release" / APP_NAME
    exe = Path(exe) if exe else root / "dist" / "zaizaishu-agent.exe"
    if not exe.is_file():
        raise FileNotFoundError(
            f"找不到已构建的 exe：{exe}\n先跑：python -m PyInstaller zaizaishu.spec --clean --noconfirm")

    plan = [("exe", exe, out / EXE_NAME)]
    plan.append(("config", root / "config.yaml", out / "config.yaml"))
    plan.append(("blockwords", root / "blockwords.txt", out / "blockwords.txt"))
    for name in ("一键安装虚拟声卡.bat", "VBCABLE_Setup_x64.exe"):
        plan.append(("vbcable", root / "assets" / "vbcable" / name, out / name))
    plan.append(("seed", root / "assets" / "seed_profile.zip",
                 out / "assets" / "seed_profile.zip"))
    for name in ("config.json", "voice_library.json"):
        plan.append(("voice", root / "voice_clone" / "voice_clone_data" / name,
                     out / "voice_clone" / "voice_clone_data" / name))
    if with_uploads:
        plan.append(("voice", root / "voice_clone" / "voice_clone_data" / "uploads",
                     out / "voice_clone" / "voice_clone_data" / "uploads"))

    done = []
    for kind, src, dst in plan:
        if not src.exists():
            raise FileNotFoundError(f"发布包素材缺失（{kind}）：{src}")
        done.append((str(dst.relative_to(out)), f"{kind}  ← {src.relative_to(root)}"))
        if dry_run:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
    return done


def package_size(out_dir: Path) -> int:
    return sum(p.stat().st_size for p in Path(out_dir).rglob("*") if p.is_file())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="组装发布包（exe + 配置 + 虚拟声卡 + 外置种子）")
    p.add_argument("--out", default="", help="输出目录（默认 dist/release/<产品名>）")
    p.add_argument("--exe", default="", help="已构建的 exe 路径（默认 dist/zaizaishu-agent.exe）")
    p.add_argument("--with-uploads", action="store_true", help="连音色克隆素材 uploads/ 一起带")
    p.add_argument("--dry-run", action="store_true", help="只列清单不落盘")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    out = Path(args.out) if args.out else ROOT / "dist" / "release" / APP_NAME
    try:
        done = build_release(out, exe=Path(args.exe) if args.exe else None,
                             dry_run=args.dry_run, with_uploads=args.with_uploads)
    except FileNotFoundError as exc:
        print(f"[!] {exc}")
        return EXIT_MISSING

    print(f"发布包目录：{out}{'（dry-run，未落盘）' if args.dry_run else ''}")
    for rel, note in done:
        print(f"  + {rel:42s} {note}")
    if not args.dry_run:
        total = package_size(out)
        print(f"\n合计 {total:,} B = {total / 1024 / 1024:.2f} MB")
        print("解压后双击「%s」即可（首跑自动落地种子存档）。" % EXE_NAME)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
