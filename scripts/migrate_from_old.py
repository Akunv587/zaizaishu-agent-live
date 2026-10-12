# -*- coding: utf-8 -*-
"""migrate_from_old.py —— 从旧版归档目录迁移用户数据到新版本（M7）。

**只复制，绝不改写/删除源目录**（连 mtime 都不碰）。

迁移内容：

    profiles/<pid>/{profile.yaml, audio/}   →  新版 profiles/
    voice_clone/voice_clone_data/           →  新版 voice_clone/voice_clone_data/
        （config.json / voice_library.json / uploads/*）

校验（决定退出码）：

  * `profile.yaml` 可被 YAML 解析，且 `version` ∈ {2, 3, 4}
  * 引用的每个音频文件在 `<pid>/audio/` 下**存在且非空**  → 缺失 = **硬错误**
  * 引用的 `voice_id` 能在 `voice_library.json` 里找到   → 找不到 = **告警**（音色可能稍后再加）

不做 v3 → v4 结构转换：那一步由新版 `hub/scriptstore.py` 的幂等迁移在**首次加载时**完成
（并留 `profile.yaml.v3.bak` 备份）。本脚本只负责把文件原样搬过去 + 搬之前验一遍。

退出码：0 = 成功（可有告警）；1 = 硬错误；2 = 参数 / 源目录不合法。

用法：
    python scripts/migrate_from_old.py                     # 用默认归档路径
    python scripts/migrate_from_old.py --dry-run           # 只看会做什么
    python scripts/migrate_from_old.py --src <旧版app目录> --dst <新版app目录>
    python scripts/migrate_from_old.py --force             # 目标已存在时覆盖
    python scripts/migrate_from_old.py --verify            # 迁移后用 ScriptStore 试加载
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - 正常环境装了 pyyaml
    yaml = None

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SRC = ROOT.parent / "_old-zaizaishu-live-20260921" / "zaizaishu-live"

PROFILES_REL = "profiles"
VOICE_REL = Path("voice_clone") / "voice_clone_data"
VOICE_LIBRARY_NAME = "voice_library.json"
SUPPORTED_VERSIONS = (2, 3, 4)
EXIT_OK, EXIT_HARD, EXIT_ARGS = 0, 1, 2


class MigrationError(Exception):
    """源目录不合法或存档损坏（硬错误）。"""


# ---------------------------------------------------------------- 读取与解析

def load_yaml(path: Path) -> dict:
    if yaml is None:  # pragma: no cover
        raise MigrationError("缺少 pyyaml 依赖（pip install pyyaml）")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MigrationError(f"读不到 {path}：{exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise MigrationError(f"{path} 不是合法 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise MigrationError(f"{path} 顶层不是映射（实际 {type(data).__name__}）")
    return data


def audio_refs(profile: dict) -> list:
    """收集存档引用的 (voice_id, 文件名) 列表。

    实现上遍历**所有列表型顶层键**（`samples_ai` / `topics_ai` / 更老的 `samples` /
    `topics` …），只认条目里的 `audios`——不写死桶名，避免"新版把 topics 改名
    topics_ai 后漏统计"这类静默少算。

    同时兼容两种 audios 形状：
      v3 平面：``audios: {voice_id: {file: x.wav, ...}}``
      v4 二维：``audios: {voice_id: {emotion: {file: x.wav, ...}}}``
    """
    refs: list = []
    for items in profile.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            audios = item.get("audios")
            if not isinstance(audios, dict):
                continue
            for voice_id, slots in audios.items():
                refs.extend(_refs_in_slots(str(voice_id), slots))
    return refs


def _refs_in_slots(voice_id: str, slots) -> list:
    out: list = []
    if not isinstance(slots, dict):
        return out
    if "file" in slots or "text_hash" in slots:            # v3 平面
        name = slots.get("file")
        if name:
            out.append((voice_id, str(name)))
    else:                                                  # v4 二维
        for _emotion, slot in slots.items():
            if isinstance(slot, dict) and slot.get("file"):
                out.append((voice_id, str(slot["file"])))
    return out


def collect_voice_ids(profile: dict) -> set:
    ids = set()
    voices = profile.get("voices")
    if isinstance(voices, list):
        for v in voices:
            if isinstance(v, dict) and v.get("voice_id"):
                ids.add(str(v["voice_id"]))
    cur = profile.get("current_voice")
    if cur:
        ids.add(str(cur))
    return ids


def load_voice_library(path: Path) -> set:
    """从 voice_library.json 里挑出所有 voice_id（文件不存在返回空集合）。"""
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrationError(f"{path} 不是合法 JSON：{exc}") from exc
    ids = set()
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict) and row.get("voice_id"):
                ids.add(str(row["voice_id"]))
    elif isinstance(data, dict):
        for row in data.get("voices", []) or []:
            if isinstance(row, dict) and row.get("voice_id"):
                ids.add(str(row["voice_id"]))
    return ids


# ------------------------------------------------------------------ 检查存档

def inspect_profile(pid_dir: Path, known_voices=None) -> dict:
    """检查一个存档目录，返回报告 dict（不抛异常，问题都落在报告里）。"""
    pid = pid_dir.name
    report = {
        "pid": pid, "ok": True, "errors": [], "warnings": [],
        "version": None, "name": "", "refs": 0,
        "missing_audio": [], "empty_audio": [], "unknown_voices": [],
        "audio_dir": None,
    }
    yaml_path = pid_dir / "profile.yaml"
    if not yaml_path.is_file():
        report["ok"] = False
        report["errors"].append("缺 profile.yaml")
        return report
    try:
        profile = load_yaml(yaml_path)
    except MigrationError as exc:
        report["ok"] = False
        report["errors"].append(str(exc))
        return report

    version = profile.get("version")
    report["version"] = version
    report["name"] = str(profile.get("name") or "")
    if version not in SUPPORTED_VERSIONS:
        report["ok"] = False
        report["errors"].append(f"存档版本 {version!r} 不在支持范围 {SUPPORTED_VERSIONS}")

    # 音频目录：新版叫 audio/，兼容个别老版本的 audios/
    audio_dir = None
    for candidate in ("audio", "audios"):
        d = pid_dir / candidate
        if d.is_dir():
            audio_dir = d
            break
    report["audio_dir"] = audio_dir.name if audio_dir else None

    refs = audio_refs(profile)
    report["refs"] = len(refs)
    for voice_id, name in refs:
        target = (audio_dir / name) if audio_dir else None
        if target is None or not target.is_file():
            report["missing_audio"].append(name)
        elif target.stat().st_size == 0:
            report["empty_audio"].append(name)

    if known_voices is not None:
        for vid in sorted(collect_voice_ids(profile)):
            if vid not in known_voices:
                report["unknown_voices"].append(vid)

    if report["missing_audio"] or report["empty_audio"]:
        report["ok"] = False
        if report["missing_audio"]:
            report["errors"].append(f"{len(report['missing_audio'])} 个引用音频缺失")
        if report["empty_audio"]:
            report["errors"].append(f"{len(report['empty_audio'])} 个引用音频为空文件")
    if report["unknown_voices"]:
        report["warnings"].append(
            f"音色库中找不到：{'、'.join(report['unknown_voices'])}")
    return report


def inspect_source(src_root: Path, extra_voices=None) -> dict:
    """检查整个源应用目录，返回 {"profiles": [report...], "voice": report, ...}。

    ``extra_voices``：目标端已登记的音色 id 集合。旧版音色库常常缺"后来才加"的音色，
    而目标端可能已经有——校验时取并集，避免把正常情况报成告警。
    """
    if not src_root.is_dir():
        raise MigrationError(f"源目录不存在：{src_root}")
    profiles_dir = src_root / PROFILES_REL
    voice_dir = src_root / VOICE_REL
    if not profiles_dir.is_dir() and not voice_dir.is_dir():
        raise MigrationError(
            f"{src_root} 既没有 {PROFILES_REL}/ 也没有 {VOICE_REL}/，不像旧版应用目录")

    known_voices = load_voice_library(voice_dir / VOICE_LIBRARY_NAME)
    if extra_voices:
        known_voices = known_voices | set(extra_voices)
    reports = []
    if profiles_dir.is_dir():
        for pid_dir in sorted(p for p in profiles_dir.iterdir() if p.is_dir()):
            reports.append(inspect_profile(pid_dir, known_voices))

    voice = {"exists": voice_dir.is_dir(), "voices": len(known_voices),
             "error": "", "uploads": 0}
    if voice_dir.is_dir():
        lib = voice_dir / VOICE_LIBRARY_NAME
        if not lib.is_file():
            voice["error"] = f"缺 {VOICE_LIBRARY_NAME}"
        uploads = voice_dir / "uploads"
        voice["uploads"] = len(list(uploads.glob("*"))) if uploads.is_dir() else 0
    return {"src": src_root, "profiles": reports, "voice": voice,
            "known_voices": known_voices}


# -------------------------------------------------------------------- 复制

def copy_profile(pid_dir: Path, dst_root: Path, force: bool, dry_run: bool) -> str:
    """复制单个存档目录。返回 "copied" / "skipped" / "would-copy" / "would-skip"。"""
    dst = dst_root / PROFILES_REL / pid_dir.name
    exists = dst.exists()
    if exists and not force:
        return "would-skip" if dry_run else "skipped"
    if dry_run:
        return "would-copy"
    dst.mkdir(parents=True, exist_ok=True)
    # profile.yaml
    shutil.copy2(pid_dir / "profile.yaml", dst / "profile.yaml")
    # 音频目录（保留旧名，新版两种都能读）
    for name in ("audio", "audios"):
        src_audio = pid_dir / name
        if src_audio.is_dir():
            dst_audio = dst / name
            if dst_audio.exists() and force:
                shutil.rmtree(dst_audio)
            shutil.copytree(src_audio, dst_audio, dirs_exist_ok=True)
    return "copied"


def copy_voice_data(src_root: Path, dst_root: Path, force: bool, dry_run: bool) -> str:
    src = src_root / VOICE_REL
    if not src.is_dir():
        return "absent"
    dst = dst_root / VOICE_REL
    if dry_run:
        return "would-copy"
    dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(src.iterdir()):
        target = dst / item.name
        if item.is_dir():
            # uploads/ 只补缺（不覆盖目标已有素材，除非 --force）
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            if target.exists() and not force:
                continue           # 目标已有（可能更新）的文件一律不动
            shutil.copy2(item, target)
    return "copied"


# ---------------------------------------------------------------- 迁移主流程

def migrate(src_root: Path, dst_root: Path, *, force=False, dry_run=False,
            skip_profiles=False, skip_voice=False) -> dict:
    """执行迁移，返回结果 dict（不打印）。"""
    extra_voices = load_voice_library(dst_root / VOICE_REL / VOICE_LIBRARY_NAME)
    inspection = inspect_source(src_root, extra_voices)
    result = {"src": str(src_root), "dst": str(dst_root), "dry_run": dry_run,
              "profiles": [], "voice": "absent", "hard_errors": [], "warnings": []}

    if not skip_profiles:
        for rep in inspection["profiles"]:
            pid_dir = src_root / PROFILES_REL / rep["pid"]
            action = copy_profile(pid_dir, dst_root, force, dry_run)
            rep = dict(rep)
            rep["action"] = action
            result["profiles"].append(rep)

    if not skip_voice:
        if not inspection["voice"]["exists"]:
            result["warnings"].append(f"源目录没有 {VOICE_REL}，跳过音色数据")
        elif inspection["voice"]["error"]:
            result["voice"] = "error"
            result["hard_errors"].append(f"音色数据：{inspection['voice']['error']}")
        else:
            result["voice"] = copy_voice_data(src_root, dst_root, force, dry_run)

    for rep in inspection["profiles"]:
        for err in rep["errors"]:
            result["hard_errors"].append(f"[{rep['pid']}] {err}")
        for warn in rep["warnings"]:
            result["warnings"].append(f"[{rep['pid']}] {warn}")
    return result


def verify_migrated(dst_root: Path, pids) -> list:
    """用新版 ScriptStore 试加载迁移后的存档（会触发 v3→v4 幂等迁移）。

    代码永远取自**本仓库**（`ROOT/hub`），数据取自 ``dst_root/profiles``——
    这样即使把数据迁到别处（临时目录/发布包目录）也能验。
    """
    import importlib
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    scriptstore = importlib.import_module("hub.scriptstore")
    store = scriptstore.ScriptStore(root=Path(dst_root) / PROFILES_REL)
    out = []
    for pid in pids:
        row = {"pid": pid, "ok": True, "version": None, "entries": 0,
               "with_audio": 0, "playable": 0, "errors": []}
        audio_dir = Path(dst_root) / PROFILES_REL / pid / "audio"
        try:
            profile = store.get_profile(pid)
            row["version"] = profile.get("version")
            items = []
            for bucket in scriptstore.SECTIONS:
                items.extend(profile.get(bucket) or [])
            row["entries"] = len(items)
            for item in items:
                slots = item.get("audios") or {}
                names = [str(s.get("file")) for s in _slot_dicts(slots) if s.get("file")]
                if names:
                    row["with_audio"] += 1
                if any((audio_dir / n).is_file() and (audio_dir / n).stat().st_size > 0
                       for n in names):
                    row["playable"] += 1
            if row["version"] != scriptstore.PROFILE_VERSION:
                row["ok"] = False
                row["errors"].append(
                    f"加载后版本 {row['version']} != {scriptstore.PROFILE_VERSION}")
            if row["entries"] and row["playable"] < row["entries"]:
                row["ok"] = False
                row["errors"].append(
                    f"{row['entries'] - row['playable']} 个条目在磁盘上找不到可用音频")
        except Exception as exc:  # noqa: BLE001 - 迁移校验要兜住一切
            row["ok"] = False
            row["errors"].append(f"{type(exc).__name__}: {exc}")
        out.append(row)
    return out


def _slot_dicts(audios: dict) -> list:
    """把 v4 二维 audios 摊平成 slot dict 列表（同时也接受 v3 平面形状）。"""
    out = []
    for slots in (audios or {}).values():
        if not isinstance(slots, dict):
            continue
        if "file" in slots or "text_hash" in slots:
            out.append(slots)
        else:
            out.extend(s for s in slots.values() if isinstance(s, dict))
    return out


# --------------------------------------------------------------------- 输出

def render(result: dict, verification=None) -> str:
    lines = []
    tag = "（dry-run，未落盘）" if result["dry_run"] else ""
    lines.append(f"源目录：{result['src']}")
    lines.append(f"目标：  {result['dst']}{tag}")
    lines.append("")
    lines.append("存档：")
    if not result["profiles"]:
        lines.append("  （无）")
    for rep in result["profiles"]:
        ver = rep["version"]
        head = (f"  · {rep['pid']}  v{ver}  「{rep['name']}」  引用音频 {rep['refs']} 个"
                f"  → {rep['action']}")
        lines.append(head)
        for err in rep["errors"]:
            lines.append(f"      [错误] {err}")
        for warn in rep["warnings"]:
            lines.append(f"      [告警] {warn}")
    lines.append("")
    lines.append(f"音色数据：{result['voice']}")
    if verification:
        lines.append("")
        lines.append("迁移后试加载（ScriptStore）：")
        for row in verification:
            flag = "OK  " if row["ok"] else "FAIL"
            lines.append(f"  [{flag}] {row['pid']}  v{row['version']}  "
                         f"条目 {row['entries']}（带音频 {row['with_audio']} / "
                         f"磁盘可取 {row['playable']}）")
            for err in row["errors"]:
                lines.append(f"         {err}")
    lines.append("")
    if result["warnings"]:
        lines.append(f"告警 {len(result['warnings'])} 条：")
        for w in result["warnings"]:
            lines.append(f"  - {w}")
    if result["hard_errors"]:
        lines.append(f"硬错误 {len(result['hard_errors'])} 条：")
        for e in result["hard_errors"]:
            lines.append(f"  - {e}")
        lines.append("")
        lines.append("结论：失败（源目录未改动）")
    else:
        lines.append("结论：成功（源目录未改动）")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="从旧版归档目录迁移 profiles/ 与 voice_clone_data/（只复制不改源）")
    p.add_argument("--src", default=str(DEFAULT_SRC), help="旧版应用目录")
    p.add_argument("--dst", default=str(ROOT), help="新版应用目录（默认仓库根）")
    p.add_argument("--force", action="store_true", help="目标已存在时覆盖")
    p.add_argument("--dry-run", action="store_true", help="只检查与打印，不落盘")
    p.add_argument("--skip-profiles", action="store_true")
    p.add_argument("--skip-voice", action="store_true")
    p.add_argument("--verify", action="store_true",
                   help="迁移后用新版 ScriptStore 试加载（触发 v3→v4 迁移）")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    src, dst = Path(args.src).resolve(), Path(args.dst).resolve()
    try:
        result = migrate(src, dst, force=args.force, dry_run=args.dry_run,
                         skip_profiles=args.skip_profiles, skip_voice=args.skip_voice)
    except MigrationError as exc:
        print(f"[!] {exc}")
        return EXIT_ARGS

    verification = None
    if args.verify and not args.dry_run and not result["hard_errors"]:
        pids = [r["pid"] for r in result["profiles"]
                if r["action"] in ("copied", "skipped")]
        if pids:
            verification = verify_migrated(dst, pids)

    print(render(result, verification))
    if result["hard_errors"]:
        return EXIT_HARD
    if verification and any(not row["ok"] for row in verification):
        return EXIT_HARD
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
