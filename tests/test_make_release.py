"""scripts/make_release.py 与「种子外置」的单元测试（M7）。

要点：
  * 发布包清单与 §四 一致，且**不带 `profiles/`**（首跑靠外置种子落地）；
  * 「种子选址」是接缝：exe 旁 `assets/seed_profile.zip` 必须能被
    `hub.scriptstore.find_seed_zip()` 找到并真的解出存档；
  * `zaizaishu.spec` **不得**再把种子 zip 内联进 exe（否则体积从 ~23 MB 涨到 ~92 MB）。
"""
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import make_release as rel  # noqa: E402

import hub.scriptstore as ss  # noqa: E402
from hub.scriptstore import ScriptStore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------- 发布包组装

def _fake_root(tmp_path, with_uploads=False):
    """造一个够用的"项目根"，用于跑 build_release。"""
    root = tmp_path / "proj"
    (root / "dist").mkdir(parents=True)
    (root / "dist" / "zaizaishu-agent.exe").write_bytes(b"MZfake")
    (root / "config.yaml").write_text("app:\n  name: x\n", encoding="utf-8")
    (root / "blockwords.txt").write_text("脏话\n", encoding="utf-8")
    (root / "assets" / "vbcable").mkdir(parents=True)
    for n in ("一键安装虚拟声卡.bat", "VBCABLE_Setup_x64.exe"):
        (root / "assets" / "vbcable" / n).write_bytes(b"vb")
    (root / "assets" / "seed_profile.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    vd = root / "voice_clone" / "voice_clone_data"
    vd.mkdir(parents=True)
    (vd / "config.json").write_text('{"api_key": "sk-x"}', encoding="utf-8")
    (vd / "voice_library.json").write_text("[]", encoding="utf-8")
    if with_uploads:
        (vd / "uploads").mkdir()
        (vd / "uploads" / "a.m4a").write_bytes(b"m4a")
    (root / "profiles" / "p_shouldnotcopy").mkdir(parents=True)
    (root / "profiles" / "p_shouldnotcopy" / "profile.yaml").write_text("version: 4\n",
                                                                       encoding="utf-8")
    return root


def test_build_release_layout(tmp_path):
    root = _fake_root(tmp_path)
    out = root / "dist" / "release" / rel.APP_NAME
    rel.build_release(out, root=root)
    names = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert names == sorted([
        rel.EXE_NAME, "config.yaml", "blockwords.txt",
        "一键安装虚拟声卡.bat", "VBCABLE_Setup_x64.exe",
        "assets/seed_profile.zip",
        "voice_clone/voice_clone_data/config.json",
        "voice_clone/voice_clone_data/voice_library.json",
    ])
    assert (out / rel.EXE_NAME).read_bytes() == b"MZfake"


def test_build_release_excludes_profiles(tmp_path):
    root = _fake_root(tmp_path)
    out = root / "dist" / "release" / rel.APP_NAME
    rel.build_release(out, root=root)
    assert not (out / "profiles").exists()


def test_build_release_uploads_opt_in(tmp_path):
    root = _fake_root(tmp_path, with_uploads=True)
    out = root / "dist" / "release" / rel.APP_NAME
    rel.build_release(out, root=root)                      # 默认不带
    assert not (out / "voice_clone" / "voice_clone_data" / "uploads").exists()
    rel.build_release(out, root=root, with_uploads=True)   # 显式带上
    assert (out / "voice_clone" / "voice_clone_data" / "uploads" / "a.m4a").is_file()


def test_build_release_dry_run_writes_nothing(tmp_path):
    root = _fake_root(tmp_path)
    out = root / "dist" / "release" / rel.APP_NAME
    plan = rel.build_release(out, root=root, dry_run=True)
    assert len(plan) >= 7
    assert not out.exists()


def test_build_release_missing_exe_raises(tmp_path):
    root = _fake_root(tmp_path)
    (root / "dist" / "zaizaishu-agent.exe").unlink()
    with pytest.raises(FileNotFoundError):
        rel.build_release(root / "out", root=root)


def test_build_release_missing_material_raises(tmp_path):
    root = _fake_root(tmp_path)
    (root / "assets" / "vbcable" / "VBCABLE_Setup_x64.exe").unlink()
    with pytest.raises(FileNotFoundError):
        rel.build_release(root / "out", root=root)


def test_main_cli_missing_exe_exit_1(tmp_path, capsys):
    rc = rel.main(["--out", str(tmp_path / "rel"),
                   "--exe", str(tmp_path / "没有这个.exe")])
    assert rc == rel.EXIT_MISSING
    assert "[!]" in capsys.readouterr().out


def test_main_cli_dry_run_exit_0(tmp_path, capsys):
    """用真仓库素材跑 dry-run（只列清单不落盘），退出码 0。"""
    needed = [
        ROOT / "dist" / "zaizaishu-agent.exe",
        ROOT / "config.yaml",
        ROOT / "blockwords.txt",
        rel.SEED_ZIP,
        ROOT / "assets" / "vbcable" / "VBCABLE_Setup_x64.exe",
        ROOT / "voice_clone" / "voice_clone_data" / "config.json",
    ]
    if not all(p.exists() for p in needed):
        pytest.skip("本仓库为脱敏公开版，未附带 exe / 种子存档 / 虚拟声卡安装包 / 真实配置等发布素材，"
                    "跳过真仓库 dry-run")
    rc = rel.main(["--out", str(tmp_path / "rel"), "--dry-run"])
    assert rc == rel.EXIT_OK
    out = capsys.readouterr().out
    assert "seed_profile.zip" in out and "dry-run" in out
    assert not (tmp_path / "rel").exists()


# --------------------------------------------------------------- 种子外置

def test_find_seed_zip_falls_back_to_exe_dir(tmp_path, monkeypatch):
    """包内路径不存在时，必须能落到 exe / 项目根旁的 assets/。"""
    bundle_zip = tmp_path / "bundle" / "assets" / "seed_profile.zip"
    exe_zip = tmp_path / "exe_dir" / "assets" / "seed_profile.zip"
    exe_zip.parent.mkdir(parents=True)
    exe_zip.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    monkeypatch.setattr(ss, "SEED_ZIP", bundle_zip)
    monkeypatch.setattr(ss, "EXE_DIR", tmp_path / "exe_dir")
    assert ss.find_seed_zip() == exe_zip


def test_find_seed_zip_prefers_bundled(tmp_path, monkeypatch):
    bundle_zip = tmp_path / "bundle" / "assets" / "seed_profile.zip"
    exe_zip = tmp_path / "exe_dir" / "assets" / "seed_profile.zip"
    for p in (bundle_zip, exe_zip):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    monkeypatch.setattr(ss, "SEED_ZIP", bundle_zip)
    monkeypatch.setattr(ss, "EXE_DIR", tmp_path / "exe_dir")
    assert ss.find_seed_zip() == bundle_zip


def test_find_seed_zip_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SEED_ZIP", tmp_path / "a.zip")
    monkeypatch.setattr(ss, "EXE_DIR", tmp_path / "empty")
    assert ss.find_seed_zip() is None


def test_seed_from_external_assets_dir(tmp_path, monkeypatch):
    """接缝：exe 旁的外置种子必须能真的解出存档（首跑落地）。"""
    src_store = ScriptStore(root=tmp_path / "src")
    pid = src_store.create_profile("外置种子")["id"]
    prof = src_store.get_profile(pid)
    prof["samples_ai"] = [{"id": "s1", "type_name": "问满级", "examples": ["满级"],
                           "reply_text": "七十级", "enabled": True, "audios": {}}]
    src_store.save_profile(pid, prof)
    zpath = tmp_path / "external" / "assets" / "seed_profile.zip"
    zpath.parent.mkdir(parents=True)
    src_store.export_profile(pid, zpath)

    monkeypatch.setattr(ss, "SEED_ZIP", tmp_path / "bundle" / "assets" / "seed_profile.zip")
    monkeypatch.setattr(ss, "EXE_DIR", tmp_path / "external")

    dst = ScriptStore(root=tmp_path / "dst")
    seeded = ss.seed_profiles_if_empty(dst)
    assert seeded
    got = dst.get_profile(seeded)
    assert got["name"] == "外置种子"
    assert len(got["samples_ai"]) == 1
    assert ss.seed_profiles_if_empty(dst) == ""        # 幂等：已有存档不再种


def test_spec_does_not_inline_seed_zip():
    """回归：种子 zip 不许再进 exe（否则 exe 从 ~23 MB 涨到 ~92 MB）。"""
    spec = (ROOT / "zaizaishu.spec").read_text(encoding="utf-8")
    assert "seed_profile.zip" in spec          # 注释里说明为什么不带
    assert "('assets/seed_profile.zip'" not in spec
    assert "'assets/seed_profile.zip'," not in spec


def test_shipped_seed_zip_still_present():
    """外置的前提是发布包里仍有这份种子——它必须还在 assets/。"""
    if not rel.SEED_ZIP.is_file():
        pytest.skip("本仓库为脱敏公开版，未附带 assets/seed_profile.zip（含真人音色样本），跳过")
    with zipfile.ZipFile(rel.SEED_ZIP) as zf:
        assert "profile.yaml" in zf.namelist()
