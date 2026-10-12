"""scripts/migrate_from_old.py 旧存档迁移单测（M7）。

口径：
  * 「只复制不改源」是硬约束——复制后源目录的文件（含 mtime、字节）必须一致；
  * 校验分两级：引用音频缺失 = 硬错误（退出码 1）；音色库查不到 = 告警（仍成功）；
  * `verify_migrated` 是**接缝测试**：用真 `hub.scriptstore.ScriptStore` 加载迁移后的
    数据（触发 v3→v4 幂等迁移），断言条目数与"磁盘上真能取到音频"的条目数。
"""
import sys
from pathlib import Path

import pytest
import yaml

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import migrate_from_old as mig  # noqa: E402

from hub.scriptstore import PROFILE_VERSION, ScriptStore  # noqa: E402

PID = "p_oldtest01"


# ------------------------------------------------------------------ 夹具构造

def v3_profile(name="旧主播"):
    return {
        "version": 3,
        "name": name,
        "voices": [{"voice_id": "vA", "label": "标准", "params": {}}],
        "current_voice": "vA",
        "knowledge": {"nickname": name, "style": "口语", "max_reply_chars": 40,
                      "game_name": "G", "game_intro": "i", "download_hint": "d",
                      "highlights": []},
        "samples_ai": [
            {"id": "s1", "type_name": "问满级", "examples": ["满级多少"],
             "reply_text": "七十级", "enabled": True,
             "audios": {"vA": {"file": "a1.wav", "text_hash": "h1"}}},
            {"id": "s2", "type_name": "问下载", "examples": ["哪下"],
             "reply_text": "点小手柄", "enabled": True,
             "audios": {"vA": {"file": "a2.wav", "text_hash": "h2"}}},
        ],
        "topics_ai": [
            {"id": "t1", "title": "话题一", "text": "聊聊新区", "enabled": True,
             "audios": {"vA": {"file": "a3.wav", "text_hash": "h3"}}},
        ],
    }


def build_old_app(root: Path, profile=None, audio_files=("a1.wav", "a2.wav", "a3.wav"),
                  voices=("vA",), pid=PID, write_voice_data=True):
    """在 root 下造一个"旧版应用目录"。音频写成非空 wav。"""
    pdir = root / "profiles" / pid
    (pdir / "audio").mkdir(parents=True, exist_ok=True)
    data = v3_profile() if profile is None else profile
    (pdir / "profile.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    for name in audio_files:
        (pdir / "audio" / name).write_bytes(b"RIFF0000WAVEfmt ")
    if write_voice_data:
        vdir = root / "voice_clone" / "voice_clone_data"
        (vdir / "uploads").mkdir(parents=True, exist_ok=True)
        (vdir / "voice_library.json").write_text(
            __import__("json").dumps([{"voice_id": v} for v in voices]),
            encoding="utf-8")
        (vdir / "config.json").write_text('{"a": 1}', encoding="utf-8")
        (vdir / "uploads" / "u1.m4a").write_bytes(b"m4a")
    return data


def snapshot_tree(root: Path) -> dict:
    """目录快照：相对路径 → (大小, mtime_ns)。用于断言"源目录未被改动"。"""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
    return out


# ------------------------------------------------------------------ 纯函数

def test_audio_refs_flat_v3():
    refs = mig.audio_refs(v3_profile())
    assert sorted(refs) == [("vA", "a1.wav"), ("vA", "a2.wav"), ("vA", "a3.wav")]


def test_audio_refs_nested_v4():
    profile = {"samples_ai": [
        {"id": "s", "audios": {"vA": {"default": {"file": "d.wav", "text_hash": "x"},
                                      "happy": {"file": "h.wav", "text_hash": "x"}}}}]}
    assert sorted(mig.audio_refs(profile)) == [("vA", "d.wav"), ("vA", "h.wav")]


def test_audio_refs_covers_every_list_bucket():
    """回归：只写死桶名会漏统计（v4 把 topics 改名 topics_ai）。"""
    profile = {"samples_ai": [{"audios": {"vA": {"file": "s.wav"}}}],
               "topics_ai": [{"audios": {"vA": {"file": "t.wav"}}}],
               "whatever_future_key": [{"audios": {"vA": {"file": "f.wav"}}}]}
    assert sorted(n for _, n in mig.audio_refs(profile)) == ["f.wav", "s.wav", "t.wav"]


def test_audio_refs_ignores_non_dict_items_and_missing_audios():
    profile = {"samples_ai": ["不是字典", {"id": "x"}, {"audios": "不是字典"},
                              {"audios": {"vA": {"file": ""}}}]}
    assert mig.audio_refs(profile) == []


def test_collect_voice_ids_includes_current():
    ids = mig.collect_voice_ids({"voices": [{"voice_id": "vA"}, {"voice_id": "vB"}],
                                 "current_voice": "vC"})
    assert ids == {"vA", "vB", "vC"}


def test_load_voice_library_missing_file_is_empty(tmp_path):
    assert mig.load_voice_library(tmp_path / "nope.json") == set()


def test_load_voice_library_bad_json_raises(tmp_path):
    bad = tmp_path / "voice_library.json"
    bad.write_text("{ 这不是 json", encoding="utf-8")
    with pytest.raises(mig.MigrationError):
        mig.load_voice_library(bad)


def test_load_voice_library_both_shapes(tmp_path):
    p = tmp_path / "voice_library.json"
    p.write_text('[{"voice_id": "v1"}, {"file_id": 1}, "垃圾"]', encoding="utf-8")
    assert mig.load_voice_library(p) == {"v1"}
    p.write_text('{"voices": [{"voice_id": "v2"}]}', encoding="utf-8")
    assert mig.load_voice_library(p) == {"v2"}


# -------------------------------------------------------------- inspect_profile

def test_inspect_profile_ok(tmp_path):
    build_old_app(tmp_path)
    rep = mig.inspect_profile(tmp_path / "profiles" / PID, {"vA"})
    assert rep["ok"] is True
    assert rep["version"] == 3
    assert rep["name"] == "旧主播"
    assert rep["refs"] == 3
    assert rep["errors"] == [] and rep["warnings"] == []


def test_inspect_profile_missing_audio_is_hard_error(tmp_path):
    build_old_app(tmp_path, audio_files=("a1.wav",))   # 缺 a2/a3
    rep = mig.inspect_profile(tmp_path / "profiles" / PID, {"vA"})
    assert rep["ok"] is False
    assert sorted(rep["missing_audio"]) == ["a2.wav", "a3.wav"]
    assert any("缺失" in e for e in rep["errors"])


def test_inspect_profile_empty_audio_is_hard_error(tmp_path):
    build_old_app(tmp_path)
    (tmp_path / "profiles" / PID / "audio" / "a2.wav").write_bytes(b"")
    rep = mig.inspect_profile(tmp_path / "profiles" / PID, {"vA"})
    assert rep["ok"] is False
    assert rep["empty_audio"] == ["a2.wav"]


def test_inspect_profile_bad_version_is_hard_error(tmp_path):
    build_old_app(tmp_path, profile={**v3_profile(), "version": 9})
    rep = mig.inspect_profile(tmp_path / "profiles" / PID, {"vA"})
    assert rep["ok"] is False
    assert any("版本" in e for e in rep["errors"])


def test_inspect_profile_unparseable_yaml(tmp_path):
    pdir = tmp_path / "profiles" / PID
    (pdir / "audio").mkdir(parents=True)
    (pdir / "profile.yaml").write_text("a: [1,\n  b: :::", encoding="utf-8")
    rep = mig.inspect_profile(pdir)
    assert rep["ok"] is False
    assert any("YAML" in e for e in rep["errors"])


def test_inspect_profile_missing_yaml(tmp_path):
    pdir = tmp_path / "profiles" / PID
    pdir.mkdir(parents=True)
    rep = mig.inspect_profile(pdir)
    assert rep["ok"] is False
    assert rep["errors"] == ["缺 profile.yaml"]


def test_inspect_profile_top_level_not_mapping(tmp_path):
    pdir = tmp_path / "profiles" / PID
    pdir.mkdir(parents=True)
    (pdir / "profile.yaml").write_text("- 1\n- 2\n", encoding="utf-8")
    rep = mig.inspect_profile(pdir)
    assert rep["ok"] is False
    assert any("顶层不是映射" in e for e in rep["errors"])


def test_inspect_profile_unknown_voice_is_warning_only(tmp_path):
    build_old_app(tmp_path)
    rep = mig.inspect_profile(tmp_path / "profiles" / PID, {"vZ"})
    assert rep["ok"] is True                      # 音色不在库里不阻断迁移
    assert rep["unknown_voices"] == ["vA"]
    assert rep["warnings"]


def test_inspect_profile_no_library_skips_voice_check(tmp_path):
    build_old_app(tmp_path)
    rep = mig.inspect_profile(tmp_path / "profiles" / PID, None)
    assert rep["unknown_voices"] == []
    assert rep["ok"] is True


# -------------------------------------------------------------- inspect_source

def test_inspect_source_rejects_non_app_dir(tmp_path):
    (tmp_path / "随便一个目录").mkdir()
    with pytest.raises(mig.MigrationError):
        mig.inspect_source(tmp_path)


def test_inspect_source_missing_dir(tmp_path):
    with pytest.raises(mig.MigrationError):
        mig.inspect_source(tmp_path / "不存在")


def test_inspect_source_extra_voices_suppress_warning(tmp_path):
    build_old_app(tmp_path, voices=("其它音色",))
    alone = mig.inspect_source(tmp_path)
    assert alone["profiles"][0]["unknown_voices"] == ["vA"]
    assert alone["voice"]["voices"] == 1          # 只数源库
    merged = mig.inspect_source(tmp_path, {"vA"})
    assert merged["profiles"][0]["unknown_voices"] == []
    assert merged["voice"]["voices"] == 2         # 源库 ∪ 目标库
    assert merged["voice"]["uploads"] == 1


# --------------------------------------------------------------------- 复制

def test_copy_profile_leaves_source_untouched(tmp_path):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src)
    before = snapshot_tree(src)
    assert mig.copy_profile(src / "profiles" / PID, dst, False, False) == "copied"
    assert snapshot_tree(src) == before           # 源一字未改（含 mtime）
    assert (dst / "profiles" / PID / "profile.yaml").is_file()
    assert (dst / "profiles" / PID / "audio" / "a3.wav").read_bytes().startswith(b"RIFF")


def test_copy_profile_skips_existing_without_force(tmp_path):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src)
    (dst / "profiles" / PID).mkdir(parents=True)
    (dst / "profiles" / PID / "profile.yaml").write_text("version: 4\n", encoding="utf-8")
    assert mig.copy_profile(src / "profiles" / PID, dst, False, False) == "skipped"
    assert (dst / "profiles" / PID / "profile.yaml").read_text(encoding="utf-8") == "version: 4\n"


def test_copy_profile_force_overwrites(tmp_path):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src)
    (dst / "profiles" / PID).mkdir(parents=True)
    (dst / "profiles" / PID / "profile.yaml").write_text("version: 4\n", encoding="utf-8")
    assert mig.copy_profile(src / "profiles" / PID, dst, True, False) == "copied"
    assert "version: 3" in (dst / "profiles" / PID / "profile.yaml").read_text(encoding="utf-8")


def test_copy_profile_dry_run(tmp_path):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src)
    assert mig.copy_profile(src / "profiles" / PID, dst, False, True) == "would-copy"
    assert not (dst / "profiles").exists()
    assert mig.copy_profile(src / "profiles" / PID, dst, False, True) == "would-copy"


def test_copy_voice_data_does_not_clobber_existing_library(tmp_path):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src, voices=("vOld",))
    lib = dst / "voice_clone" / "voice_clone_data" / "voice_library.json"
    lib.parent.mkdir(parents=True)
    lib.write_text('[{"voice_id": "vNew"}]', encoding="utf-8")
    assert mig.copy_voice_data(src, dst, False, False) == "copied"
    assert "vNew" in lib.read_text(encoding="utf-8")     # 目标库没被旧库覆盖
    # uploads / config.json 补齐
    assert (lib.parent / "uploads" / "u1.m4a").is_file()
    assert (lib.parent / "config.json").is_file()


def test_copy_voice_data_force_overwrites_library(tmp_path):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src, voices=("vOld",))
    lib = dst / "voice_clone" / "voice_clone_data" / "voice_library.json"
    lib.parent.mkdir(parents=True)
    lib.write_text('[{"voice_id": "vNew"}]', encoding="utf-8")
    mig.copy_voice_data(src, dst, True, False)
    assert "vOld" in lib.read_text(encoding="utf-8")


def test_copy_voice_data_absent(tmp_path):
    src = tmp_path / "old"
    (src / "profiles").mkdir(parents=True)
    assert mig.copy_voice_data(src, tmp_path / "new", False, False) == "absent"


# ------------------------------------------------------------------- migrate

def test_migrate_full_result(tmp_path):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src)
    result = mig.migrate(src, dst)
    assert result["hard_errors"] == []
    assert [r["action"] for r in result["profiles"]] == ["copied"]
    assert result["voice"] == "copied"
    assert (dst / "profiles" / PID / "profile.yaml").is_file()


def test_migrate_dry_run_writes_nothing(tmp_path):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src)
    result = mig.migrate(src, dst, dry_run=True)
    assert result["profiles"][0]["action"] == "would-copy"
    assert not dst.exists()


def test_migrate_collects_hard_errors(tmp_path):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src, audio_files=())            # 三个引用全缺
    result = mig.migrate(src, dst)
    assert result["hard_errors"]
    assert any("缺失" in e for e in result["hard_errors"])


def test_migrate_skip_flags(tmp_path):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src)
    result = mig.migrate(src, dst, skip_profiles=True, skip_voice=True)
    assert result["profiles"] == [] and result["voice"] == "absent"
    assert not (dst / "voice_clone").exists()


def test_migrate_warns_when_no_voice_dir(tmp_path):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src, write_voice_data=False)
    result = mig.migrate(src, dst)
    assert result["voice"] == "absent"
    assert any("跳过音色数据" in w for w in result["warnings"])


# -------------------------------------------------------------- verify（接缝）

def test_verify_migrated_uses_real_scriptstore(tmp_path):
    """真 ScriptStore 加载迁移后的 v3 存档 → 升到 v4 且每个条目磁盘可取音频。"""
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src)
    mig.migrate(src, dst)
    rows = mig.verify_migrated(dst, [PID])
    assert len(rows) == 1
    row = rows[0]
    assert row["errors"] == []
    assert row["ok"] is True
    assert row["version"] == PROFILE_VERSION == 4
    assert row["entries"] == 3            # 2 samples + 1 topic
    assert row["playable"] == 3           # 磁盘上真有非空音频
    # 真 ScriptStore 也能直接读（证明数据是"应用可消费"的形状）
    profile = ScriptStore(root=dst / "profiles").get_profile(PID)
    assert profile["version"] == PROFILE_VERSION
    assert len(profile["samples_ai"]) == 2 and len(profile["topics_ai"]) == 1


def test_verify_migrated_flags_missing_audio_on_disk(tmp_path):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src)
    mig.migrate(src, dst)
    (dst / "profiles" / PID / "audio" / "a2.wav").unlink()
    rows = mig.verify_migrated(dst, [PID])
    assert rows[0]["ok"] is False
    assert any("找不到可用音频" in e for e in rows[0]["errors"])


def test_verify_migrated_unknown_pid_reports_error(tmp_path):
    rows = mig.verify_migrated(tmp_path, ["p_不存在0001"])
    assert rows[0]["ok"] is False and rows[0]["errors"]


# ---------------------------------------------------------------------- CLI

def test_main_success_exit_0(tmp_path, capsys):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src)
    assert mig.main(["--src", str(src), "--dst", str(dst)]) == mig.EXIT_OK
    out = capsys.readouterr().out
    assert "copied" in out and "结论：成功" in out


def test_main_hard_error_exit_1(tmp_path, capsys):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src, audio_files=())
    assert mig.main(["--src", str(src), "--dst", str(dst)]) == mig.EXIT_HARD
    assert "结论：失败" in capsys.readouterr().out


def test_main_bad_src_exit_2(tmp_path, capsys):
    assert mig.main(["--src", str(tmp_path / "没有"), "--dst", str(tmp_path)]) == mig.EXIT_ARGS
    assert "[!]" in capsys.readouterr().out


def test_main_dry_run_verify_skipped(tmp_path, capsys):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src)
    assert mig.main(["--src", str(src), "--dst", str(dst), "--dry-run", "--verify"]) == mig.EXIT_OK
    assert not dst.exists()
    assert "dry-run" in capsys.readouterr().out


def test_main_verify_success(tmp_path, capsys):
    src, dst = tmp_path / "old", tmp_path / "new"
    build_old_app(src)
    rc = mig.main(["--src", str(src), "--dst", str(dst), "--verify"])
    assert rc == mig.EXIT_OK
    out = capsys.readouterr().out
    assert "迁移后试加载" in out and "[OK  ]" in out


def test_render_reports_warnings_and_errors():
    result = {"src": "S", "dst": "D", "dry_run": False, "profiles": [
        {"pid": PID, "version": 3, "name": "N", "refs": 3, "action": "copied",
         "errors": ["坏"], "warnings": ["怪"]}],
        "voice": "copied", "hard_errors": ["[%s] 坏" % PID], "warnings": ["[%s] 怪" % PID]}
    text = mig.render(result)
    assert "[错误] 坏" in text and "[告警] 怪" in text and "结论：失败" in text


def test_default_src_points_at_archive():
    assert mig.DEFAULT_SRC.name == "zaizaishu-live"
    assert "_old-zaizaishu-live" in str(mig.DEFAULT_SRC)
