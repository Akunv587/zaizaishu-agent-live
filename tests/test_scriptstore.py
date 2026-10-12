"""hub.scriptstore 话术存档库单测（话术管理 v4：单方案纯AI + 存档内多音色 × 多情绪档）。

覆盖：默认存档兜底、CRUD、复制新建（含音频）、音频校验与路径安全、
zip 导出/导入（含缺失引用落空、坏包拒绝）、归一化边界、
v1 → 最新迁移（清空重建 + 删除孤儿音频）、v2 → 最新迁移（audios 多音色映射）、
v3 → v4 迁移（扁平音频包成 default 档 + 备份 + 幂等）、
情绪档位集合归一、缺失统计、档位清空、单条合成（generate_audio）、
存档音色管理（增删改/当前音色切换/当前音色拒删）、stale 标注与防覆盖护栏。
"""
import zipfile

import pytest
import yaml

from hub.scriptstore import (
    DEFAULT_EMOTIONS,
    EMOTION_DEFAULT,
    EMOTIONS_ALL,
    PROFILE_VERSION,
    ScriptStore,
    ScriptStoreError,
    count_missing,
    default_profile,
    normalize_audios,
    normalize_emotion,
    normalize_emotions,
    normalize_profile,
    text_hash,
)


def _wav(n: int = 2048) -> bytes:
    """最小合法 wav（魔数 RIFF/WAVE + 填充到 n 字节）。"""
    return b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * (n - 12)


def _mp3(n: int = 2048) -> bytes:
    return b"ID3" + b"\x00" * (n - 3)


@pytest.fixture
def store(tmp_path):
    return ScriptStore(root=tmp_path / "profiles")


# ---------- 默认存档与兜底 ----------

def test_ensure_default_creates_once(store):
    pid = store.ensure_default()
    assert store.exists(pid)
    profile = store.get_profile(pid)
    assert profile["name"] == "默认话术"
    assert profile["schedule"] == {"enabled": True, "interval_s": 30, "jitter_s": 15}
    assert profile["knowledge"]["nickname"] == "仔仔鼠"
    assert profile["knowledge"]["max_reply_chars"] == 40
    assert len(profile["event_templates"]["gift_low"]) >= 3
    assert len(profile["event_templates"]["enter"]) >= 1
    # 幂等：第二次返回同一 id，不重复创建
    assert store.ensure_default() == pid
    assert len(store.list_profiles()) == 1


def test_default_profile_name_fallback():
    assert default_profile("")["name"] == "未命名存档"


def test_event_enabled_defaults_and_roundtrip(store):
    pid = store.create_profile("开关测试")["id"]
    p = store.get_profile(pid)
    assert p["event_enabled"] == {
        "gift_low": True, "gift_mid": True, "gift_high": True,
        "follow": True, "fansclub": True, "enter": False,
    }
    p["event_enabled"]["gift_high"] = False
    p["event_enabled"]["enter"] = True
    saved = store.save_profile(pid, p)
    assert saved["event_enabled"]["gift_high"] is False
    assert saved["event_enabled"]["enter"] is True
    # 残缺 event_enabled 回退默认（缺键补、enter 默认关）
    partial = store.save_profile(pid, {**p, "event_enabled": {"follow": False}})
    assert partial["event_enabled"]["follow"] is False
    assert partial["event_enabled"]["gift_low"] is True
    assert partial["event_enabled"]["enter"] is False


# ---------- CRUD ----------

def test_create_list_rename_delete(store):
    a = store.create_profile("游戏推广风")
    b = store.create_profile("聊天风")
    ids = [p["id"] for p in store.list_profiles()]
    assert set(ids) == {a["id"], b["id"]}

    store.rename_profile(b["id"], "深夜聊天风")
    assert store.get_profile(b["id"])["name"] == "深夜聊天风"

    store.delete_profile(a["id"])
    assert not store.exists(a["id"])
    assert not (store.root / a["id"]).exists()
    with pytest.raises(ScriptStoreError):
        store.delete_profile(a["id"])


def test_create_copy_from_source_includes_audio(store):
    src = store.create_profile("原始")
    fname = store.save_audio(src["id"], _wav(), "voice.wav")
    data = store.get_profile(src["id"])
    data["topics_ai"].append({"id": "t_1", "title": "自我介绍", "text": "大家好",
                              "audios": {"v1": {"file": fname,
                                                "text_hash": text_hash("大家好")}},
                              "enabled": True})
    store.save_profile(src["id"], data)

    dup = store.create_profile("", source_id=src["id"])
    assert dup["id"] != src["id"]
    assert dup["name"] == "原始 副本"
    assert dup["topics_ai"][0]["audios"]["v1"]["default"]["file"] == fname
    assert store.audio_path(dup["id"], fname).read_bytes() == _wav()


def test_get_profile_normalizes_without_rewrite(store):
    p = store.create_profile("归一化")
    path = store.root / p["id"] / "profile.yaml"
    before = path.read_bytes()
    loaded = store.get_profile(p["id"])
    assert loaded["schedule"]["interval_s"] == 30  # 缺省补齐
    assert path.read_bytes() == before  # 只读不写盘


def test_invalid_pid_rejected(store):
    with pytest.raises(ScriptStoreError):
        store.get_profile("../etc")
    with pytest.raises(ScriptStoreError):
        store.exists("..\\..\\x") or store.get_profile("..\\..\\x")


# ---------- 音频资产 ----------

def test_save_audio_validation(store):
    p = store.create_profile("音频")
    with pytest.raises(ScriptStoreError):
        store.save_audio(p["id"], _wav(), "song.txt")        # 扩展名不允许
    with pytest.raises(ScriptStoreError):
        store.save_audio(p["id"], b"\x00" * 2048, "bad.wav")  # 魔数不符
    with pytest.raises(ScriptStoreError):
        store.save_audio(p["id"], _wav(100), "tiny.wav")      # 太小
    with pytest.raises(ScriptStoreError):
        store.save_audio(p["id"], b"", "empty.wav")           # 空内容

    fname = store.save_audio(p["id"], _wav(), "ok.WAV")  # 扩展名大小写归一
    assert fname.endswith(".wav")
    assert store.audio_path(p["id"], fname).read_bytes() == _wav()
    assert store.audio_exists(p["id"], fname)
    store.delete_audio(p["id"], fname)
    assert not store.audio_exists(p["id"], fname)


def test_save_audio_oversize(store, monkeypatch):
    import hub.scriptstore as ss

    monkeypatch.setattr(ss, "MAX_AUDIO_BYTES", 3000)
    p = store.create_profile("超大")
    with pytest.raises(ScriptStoreError):
        store.save_audio(p["id"], _wav(4000), "big.wav")


def test_audio_path_traversal_blocked(store):
    p = store.create_profile("安全")
    for bad in ("../evil.wav", "..\\evil.wav", "a/b.wav", "", "x.wav/evil.wav"):
        with pytest.raises(ScriptStoreError):
            store.audio_path(p["id"], bad)


# ---------- zip 导出 / 导入 ----------

def test_export_import_roundtrip(store, tmp_path):
    p = store.create_profile("导出源")
    fname = store.save_audio(p["id"], _mp3(), "clip.mp3")
    data = store.get_profile(p["id"])
    data["voices"] = [{"voice_id": "v1", "label": "标准", "params": {}}]
    data["current_voice"] = "v1"
    data["topics_ai"].append({"id": "t_1", "title": "口播一", "text": "正文",
                              "audios": {"v1": {"file": fname,
                                                "text_hash": text_hash("正文")}},
                              "enabled": True})
    store.save_profile(p["id"], data)

    dest = tmp_path / "out" / "导出源.zip"
    store.export_profile(p["id"], dest)
    assert dest.is_file()
    with zipfile.ZipFile(dest) as zf:
        assert "profile.yaml" in zf.namelist()
        assert f"audio/{fname}" in zf.namelist()

    imported, warnings = store.import_profile(dest)
    assert warnings == []
    assert imported["id"] != p["id"]
    assert imported["name"] == "导出源"
    assert imported["voices"][0]["voice_id"] == "v1"
    assert imported["current_voice"] == "v1"
    assert imported["topics_ai"][0]["audios"]["v1"]["default"]["file"] == fname
    assert store.audio_path(imported["id"], fname).read_bytes() == _mp3()


def test_import_drops_missing_audio_ref(store, tmp_path):
    src = tmp_path / "缺音频.zip"
    profile = default_profile("缺音频")
    profile["voices"] = [{"voice_id": "v1", "label": "标准", "params": {}}]
    profile["topics_ai"].append({"id": "t_1", "title": "x", "text": "y",
                                 "audios": {"v1": {"file": "deadbeef01.wav",
                                                   "text_hash": text_hash("y")}},
                                 "enabled": True})
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("profile.yaml", yaml.safe_dump(profile, allow_unicode=True))
    imported, warnings = store.import_profile(src)
    assert imported["topics_ai"][0]["audios"] == {}
    assert any("音频缺失" in w for w in warnings)


def test_import_rejects_bad_zip(store, tmp_path):
    not_zip = tmp_path / "x.zip"
    not_zip.write_bytes(b"not a zip")
    with pytest.raises(ScriptStoreError):
        store.import_profile(not_zip)

    no_yaml = tmp_path / "noyaml.zip"
    with zipfile.ZipFile(no_yaml, "w") as zf:
        zf.writestr("readme.txt", "hi")
    with pytest.raises(ScriptStoreError):
        store.import_profile(no_yaml)

    bad_yaml = tmp_path / "badyaml.zip"
    with zipfile.ZipFile(bad_yaml, "w") as zf:
        zf.writestr("profile.yaml", "key: [unclosed")
    with pytest.raises(ScriptStoreError):
        store.import_profile(bad_yaml)


def test_import_skips_traversal_and_invalid_audio(store, tmp_path):
    src = tmp_path / "脏包.zip"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("profile.yaml", yaml.safe_dump(default_profile("脏包"),
                                                   allow_unicode=True))
        zf.writestr("audio/../escape.wav", _wav())          # 路径穿越
        zf.writestr("audio/nested/deep.wav", _wav())        # 嵌套目录
        zf.writestr("audio/bad.wav", b"\x00" * 2048)        # 魔数不符
    imported, warnings = store.import_profile(src)
    assert imported["name"] == "脏包"
    assert not list((store.root / imported["id"] / "audio").iterdir())
    assert len(warnings) >= 2


# ---------- 归一化边界 ----------

def test_normalize_drops_legacy_item_fields():
    """v2 条目级 audio/voice_id/topic_id 字段在 v3 归一化时整体丢弃（不兼旧）。"""
    profile = normalize_profile({
        "name": "x",
        "samples_ai": [{"type_name": "问下载", "reply_text": "点小手柄",
                        "audio": "aa11bb22.mp3", "voice_id": "v1", "topic_id": "t_1"}],
    })
    s = profile["samples_ai"][0]
    assert "topic_id" not in s
    assert "audio" not in s
    assert "voice_id" not in s
    assert s["audios"] == {}


def test_normalize_audios_multi_voice():
    """audios 多音色 × 多情绪档映射持久化；非法文件名引用丢弃；v3 扁平形态自动包 default。"""
    profile = normalize_profile({
        "name": "x",
        "samples_ai": [{"id": "s1", "type_name": "问下载", "reply_text": "点手柄",
                        "audios": {"mvoice1": {"file": "aa11bb22.mp3", "text_hash": "abc12345"},
                                   "mvoice2": {"file": "../bad.wav", "text_hash": "x"}}}],
    })
    aud = profile["samples_ai"][0]["audios"]
    assert aud == {"mvoice1": {"default": {"file": "aa11bb22.mp3",
                                           "text_hash": "abc12345"}}}
    # 已是 v4 的二维映射原样保留；非 8 枚举的档位键丢弃
    assert normalize_audios({"v1": {"default": {"file": "a1b2c3.wav", "text_hash": "h"},
                                    "happy": {"file": "d4e5f6.wav", "text_hash": "h"},
                                    "bogus": {"file": "0a0b0c.wav", "text_hash": "h"}}}) == {
        "v1": {"default": {"file": "a1b2c3.wav", "text_hash": "h"},
               "happy": {"file": "d4e5f6.wav", "text_hash": "h"}}}


def test_normalize_clamps_and_types():
    profile = normalize_profile({
        "name": 123,
        "knowledge": {"max_reply_chars": 999, "highlights": "单条卖点"},
        "schedule": {"enabled": "false", "interval_s": 0, "jitter_s": -5},
        "topics_ai": [{"title": "t", "text": "x", "keywords": "单个词",
                       "audios": {"v1": {"file": "../bad.wav"}}, "enabled": "no"}],
        "samples_template": "不是列表",
    })
    assert profile["name"] == "123"
    assert profile["knowledge"]["max_reply_chars"] == 60
    assert profile["knowledge"]["highlights"] == ["单条卖点"]
    assert profile["schedule"] == {"enabled": False, "interval_s": 1, "jitter_s": 0}
    t = profile["topics_ai"][0]
    assert "keywords" not in t              # 模板期字段整体退役
    assert t["audios"] == {}                # 非法音频引用丢弃
    assert t["enabled"] is False
    assert "samples_template" not in profile  # 模板分区已退役
    assert profile["event_templates"]["gift_mid"]  # 事件模板缺键补默认


def test_normalize_rejects_non_mapping():
    with pytest.raises(ScriptStoreError):
        normalize_profile(["不是", "字典"])


def test_normalize_drops_v1_top_level_sections():
    """v1 顶层 samples/topics 按「清空重建」决策直接丢弃。"""
    profile = normalize_profile({
        "name": "旧档", "version": 1,
        "topics": [{"title": "旧话题"}],
        "samples": [{"type_name": "旧样本"}],
    })
    assert profile["version"] == PROFILE_VERSION
    assert profile["topics_ai"] == []
    assert profile["samples_ai"] == []
    assert profile["voices"] == []
    assert profile["current_voice"] == ""


# ---------- v1 → v3 迁移 ----------

def test_migrate_v1_profiles_clears_sections_and_audio(store):
    pid = store.create_profile("旧档")["id"]
    raw = {
        "version": 1, "name": "旧档",
        "knowledge": {"nickname": "仔仔鼠", "game_name": "旧游戏"},
        "event_templates": {"gift_low": ["谢{user}"]},
        "schedule": {"enabled": True, "interval_s": 30, "jitter_s": 15},
        "topics": [{"id": "t_1", "title": "旧话题", "text": "x"}],
        "samples": [{"id": "s_1", "type_name": "旧样本"}],
    }
    (store.root / pid / "profile.yaml").write_text(
        yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    orphan = store.save_audio(pid, _wav(), "orphan.wav")

    migrated = store.migrate_v1_profiles()
    assert [m["id"] for m in migrated] == [pid]
    assert migrated[0]["cleared_topics"] == 1
    assert migrated[0]["cleared_samples"] == 1
    assert migrated[0]["removed_audio"] == 1
    assert not store.audio_exists(pid, orphan)

    after = store.get_profile(pid)
    assert after["version"] == PROFILE_VERSION
    assert after["knowledge"]["game_name"] == "旧游戏"    # 共用区块保留
    assert after["event_templates"]["gift_low"] == ["谢{user}"]
    assert after["topics_ai"] == []                      # 清空重建
    assert after["samples_ai"] == []

    # 二次迁移幂等：v3 存档不再触碰
    assert store.migrate_v1_profiles() == []


def test_migrate_v1_profiles_leaves_current_untouched(store):
    pid = store.create_profile("新档")["id"]
    p = store.get_profile(pid)
    p["topics_ai"].append({"id": "t_1", "title": "话题", "text": "x",
                           "audios": {}, "enabled": True})
    store.save_profile(pid, p)
    assert store.migrate_v1_profiles() == []
    assert len(store.get_profile(pid)["topics_ai"]) == 1


def test_migrate_v1_profiles_skips_broken(store):
    pid = store.create_profile("坏档")["id"]
    (store.root / pid / "profile.yaml").write_text("x: [", encoding="utf-8")
    assert store.migrate_v1_profiles() == []   # 损坏存档跳过，不炸


# ---------- 存档音色管理 ----------

def test_voice_add_set_update_remove(store):
    pid = store.create_profile("音色")["id"]
    # 首个音色自动成为当前直播音色
    p = store.add_voice(pid, "voiceA", "标准")
    assert p["voices"] == [{"voice_id": "voiceA", "label": "标准", "params": {}}]
    assert p["current_voice"] == "voiceA"
    # 重复添加拒绝
    with pytest.raises(ScriptStoreError):
        store.add_voice(pid, "voiceA", "重复")

    p = store.add_voice(pid, "voiceB", "情绪")
    assert p["current_voice"] == "voiceA"   # 第二个音色不抢当前位
    # 切换当前音色（必须在 voices 内）
    with pytest.raises(ScriptStoreError):
        store.set_current_voice(pid, "voiceX")
    p = store.set_current_voice(pid, "voiceB")
    assert p["current_voice"] == "voiceB"
    # 改备注 / 调参数（参数静默钳到合法域）
    p = store.update_voice(pid, "voiceA", label="标准Pro",
                           params={"speed": 9.9, "vol": 1.2, "pitch": 3, "emotion": "happy"})
    va = next(v for v in p["voices"] if v["voice_id"] == "voiceA")
    assert va["label"] == "标准Pro"
    assert va["params"] == {"speed": 2.0, "vol": 1.2, "pitch": 3, "emotion": "happy"}

    # 当前直播音色拒删
    with pytest.raises(ScriptStoreError):
        store.remove_voice(pid, "voiceB")
    # 移除音色连带删其音频
    fname = store.save_audio(pid, _wav(), "vb.wav")
    data = store.get_profile(pid)
    data["topics_ai"].append({"id": "t_1", "title": "t", "text": "x",
                              "audios": {"voiceA": {"file": fname,
                                                    "text_hash": text_hash("x")}},
                              "enabled": True})
    store.save_profile(pid, data)
    store.set_current_voice(pid, "voiceA")
    p = store.remove_voice(pid, "voiceB")
    assert [v["voice_id"] for v in p["voices"]] == ["voiceA"]
    # voiceB 无音频可删，voiceA 的音频保留
    assert store.audio_exists(pid, fname)
    with pytest.raises(ScriptStoreError):
        store.remove_voice(pid, "voiceA")   # 当前音色仍拒删


def test_remove_voice_deletes_its_audio(store):
    pid = store.create_profile("音色音频")["id"]
    store.add_voice(pid, "voiceA", "a")
    store.add_voice(pid, "voiceB", "b")
    fa = store.save_audio(pid, _wav(), "a.wav")
    fb = store.save_audio(pid, _mp3(), "b.mp3")
    data = store.get_profile(pid)
    data["topics_ai"].append({"id": "t_1", "title": "t", "text": "x",
                              "audios": {"voiceA": {"file": fa, "text_hash": text_hash("x")},
                                         "voiceB": {"file": fb, "text_hash": text_hash("x")}},
                              "enabled": True})
    store.save_profile(pid, data)
    p = store.remove_voice(pid, "voiceB")
    assert p["topics_ai"][0]["audios"].keys() == {"voiceA"}
    assert store.audio_exists(pid, fa)
    assert not store.audio_exists(pid, fb)


# ---------- v2 → v3 迁移 ----------

def test_migrate_v2_profiles_maps_audios(store):
    pid = store.create_profile("旧v2")["id"]
    fa = store.save_audio(pid, _wav(), "a.wav")
    raw = {
        "version": 2, "name": "旧v2",
        "samples_ai": [{"id": "s1", "type_name": "问下载", "reply_text": "点手柄",
                        "audio": fa, "voice_id": "voiceA", "enabled": True}],
        "topics_ai": [{"id": "t1", "title": "话题", "text": "正文",
                       "audio": "", "voice_id": "voiceB", "enabled": True}],
        "samples_template": [{"id": "st", "type_name": "模板样本", "audio": fa}],
        "topics_template": [],
    }
    (store.root / pid / "profile.yaml").write_text(
        yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")

    migrated = store.migrate_v2_profiles(tts_voice_id="voiceA",
                                         voice_labels={"voiceA": "标准", "voiceB": "情绪"})
    assert [m["id"] for m in migrated] == [pid]
    assert migrated[0]["current_voice"] == "voiceA"
    after = store.get_profile(pid)
    assert [v["voice_id"] for v in after["voices"]] == ["voiceA", "voiceB"]
    assert after["voices"][0]["label"] == "标准"
    # 样本音频归入 audios[voiceA].default；模板分区音频引用计入 removed
    # （与样本同文件，仍记丢弃名单）
    assert after["samples_ai"][0]["audios"]["voiceA"]["default"]["file"] == fa
    assert after["samples_ai"][0]["audios"]["voiceA"]["default"]["text_hash"] == \
        text_hash("点手柄")
    assert after["topics_ai"][0]["audios"] == {}   # 有音色无音频 → 空映射
    # 幂等：v3 不再迁移
    assert store.migrate_v2_profiles() == []


def test_migrate_v2_orphan_audio_dropped(store):
    """有音频无音色归属：无法归入任何音色，音频文件删除并计数。"""
    pid = store.create_profile("孤儿")["id"]
    orphan = store.save_audio(pid, _wav(), "orphan.wav")
    raw = {
        "version": 2, "name": "孤儿",
        "topics_ai": [{"id": "t1", "title": "话题", "text": "正文",
                       "audio": orphan, "voice_id": "", "enabled": True}],
    }
    (store.root / pid / "profile.yaml").write_text(
        yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    migrated = store.migrate_v2_profiles()
    assert migrated[0]["removed_audio"] == 1
    assert not store.audio_exists(pid, orphan)
    assert store.get_profile(pid)["topics_ai"][0]["audios"] == {}


# ---------- stale 标注与防覆盖护栏 ----------

def test_stale_marked_on_text_change(store):
    pid = store.create_profile("stale")["id"]
    store.add_voice(pid, "voiceA", "a")
    fname = store.save_audio(pid, _wav(), "a.wav")
    data = store.get_profile(pid)
    data["topics_ai"].append({"id": "t_1", "title": "t", "text": "原文",
                              "audios": {"voiceA": {"file": fname,
                                                    "text_hash": text_hash("原文")}},
                              "enabled": True})
    saved = store.save_profile(pid, data)
    assert saved["topics_ai"][0]["stale"] == []          # hash 匹配：不 stale
    saved["topics_ai"][0]["text"] = "改过的话术"
    saved2 = store.save_profile(pid, saved)
    assert saved2["topics_ai"][0]["stale"] == ["voiceA"]  # 文本已改：音频待更新


def test_guard_existing_audio_survives_empty_save(store):
    """整档保存冲丢 audios 时，磁盘上仍存在的音频引用被保留（防误清，逐档位判定）。"""
    pid = store.create_profile("护栏")["id"]
    store.add_voice(pid, "voiceA", "a")
    fname = store.save_audio(pid, _wav(), "a.wav")
    data = store.get_profile(pid)
    data["topics_ai"].append({"id": "t_1", "title": "t", "text": "x",
                              "audios": {"voiceA": {"file": fname,
                                                    "text_hash": text_hash("x")}},
                              "enabled": True})
    store.save_profile(pid, data)
    # 前端脏数据：同一条目 audios 被清空
    dirty = store.get_profile(pid)
    dirty["topics_ai"][0]["audios"] = {}
    saved = store.save_profile(pid, dirty)
    assert saved["topics_ai"][0]["audios"]["voiceA"]["default"]["file"] == fname
    # 显式清除（文件已删）则放行：重新取档后清空 audios 再存
    store.delete_audio(pid, fname)
    clean = store.get_profile(pid)
    clean["topics_ai"][0]["audios"] = {}
    saved2 = store.save_profile(pid, clean)
    assert saved2["topics_ai"][0]["audios"] == {}


def test_guard_existing_audio_keeps_untouched_emotion_slot(store):
    """丢一档不牵连其余档：只冲掉 happy 档，default 档引用仍被守回。"""
    pid = store.create_profile("护栏分档")["id"]
    store.add_voice(pid, "voiceA", "a")
    f_default = store.save_audio(pid, _wav(), "d.wav")
    f_happy = store.save_audio(pid, _wav(), "h.wav")
    data = store.get_profile(pid)
    data["topics_ai"].append({"id": "t_1", "title": "t", "text": "x",
                              "audios": {"voiceA": {
                                  "default": {"file": f_default,
                                              "text_hash": text_hash("x")},
                                  "happy": {"file": f_happy,
                                            "text_hash": text_hash("x")}}},
                              "enabled": True})
    store.save_profile(pid, data)
    dirty = store.get_profile(pid)
    dirty["topics_ai"][0]["audios"]["voiceA"].pop("happy")   # 只丢 happy 档
    saved = store.save_profile(pid, dirty)
    slots = saved["topics_ai"][0]["audios"]["voiceA"]
    assert slots["default"]["file"] == f_default
    assert slots["happy"]["file"] == f_happy


# ---------- v3 → v4 迁移（T1 迁移 / T2 幂等） ----------

def _write_v3_raw(store, pid: str, text: str = "正文") -> str:
    """写一份真实 v3 结构的 profile.yaml（扁平 audios），返回音频文件名。"""
    fname = store.save_audio(pid, _wav(), "v3.wav")
    raw = {
        "version": 3, "name": "旧v3",
        "voices": [{"voice_id": "voiceA", "label": "标准", "params": {}}],
        "current_voice": "voiceA",
        "knowledge": {"nickname": "仔仔鼠"},
        "samples_ai": [{"id": "s1", "type_name": "问下载", "reply_text": text,
                        "audios": {"voiceA": {"file": fname,
                                              "text_hash": text_hash(text)}},
                        "enabled": True}],
        "topics_ai": [{"id": "t1", "title": "话题", "text": "口播正文",
                       "audios": {}, "enabled": True}],
    }
    (store.root / pid / "profile.yaml").write_text(
        yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return fname


def test_migrate_v3_profiles_wraps_default_and_backs_up(store):
    """T1：v3 存档迁移 → version 4，audios[vid].default 含原值，原件备份留痕。"""
    pid = store.create_profile("旧v3")["id"]
    fname = _write_v3_raw(store, pid)
    migrated = store.migrate_v3_profiles()
    assert [m["id"] for m in migrated] == [pid]
    assert migrated[0]["backup"] == "profile.yaml.v3.bak"
    after = store.get_profile(pid)
    assert after["version"] == PROFILE_VERSION == 4
    assert after["samples_ai"][0]["audios"]["voiceA"]["default"]["file"] == fname
    assert after["samples_ai"][0]["audios"]["voiceA"]["default"]["text_hash"] == \
        text_hash("正文")
    # 备份是原件（扁平结构），可用于回滚
    bak = store.root / pid / "profile.yaml.v3.bak"
    assert yaml.safe_load(bak.read_text(encoding="utf-8"))["version"] == 3
    # default 档可播：selector 解析得到文件路径（此处直接核对文件仍在）
    assert store.audio_exists(pid, fname)


def test_migrate_v3_profiles_is_idempotent(store):
    """T2：连续迁移两次结果一致，不二次包装。"""
    pid = store.create_profile("旧v3")["id"]
    _write_v3_raw(store, pid)
    assert len(store.migrate_v3_profiles()) == 1
    first = (store.root / pid / "profile.yaml").read_text(encoding="utf-8")
    assert store.migrate_v3_profiles() == []          # 已是 v4：跳过，不写盘
    assert (store.root / pid / "profile.yaml").read_text(encoding="utf-8") == first
    aud = store.get_profile(pid)["samples_ai"][0]["audios"]
    assert set(aud["voiceA"]) == {"default"}          # 没有 default.default 这类嵌套


def test_migrate_v3_skips_v4_and_handles_flat_with_bad_version(store):
    """version 已写 4 但内容仍是扁平结构（手改/中断）的存档同样被迁移。"""
    pid = store.create_profile("半迁移")["id"]
    fname = store.save_audio(pid, _wav(), "x.wav")
    raw = {"version": 4, "name": "半迁移",
           "topics_ai": [{"id": "t1", "title": "t", "text": "正文",
                          "audios": {"v1": {"file": fname,
                                            "text_hash": text_hash("正文")}},
                          "enabled": True}]}
    (store.root / pid / "profile.yaml").write_text(
        yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    assert len(store.migrate_v3_profiles()) == 1
    after = store.get_profile(pid)
    assert after["topics_ai"][0]["audios"]["v1"]["default"]["file"] == fname


# ---------- 情绪档位（v4） ----------

def test_normalize_emotions_rules():
    assert normalize_emotions(None) == list(DEFAULT_EMOTIONS)      # 缺键 → 出厂默认
    assert normalize_emotions([]) == [EMOTION_DEFAULT]             # 显式清空只留 default
    assert normalize_emotions(["happy", "nope", "angry"]) == \
        [EMOTION_DEFAULT, "happy", "angry"]                        # 非法档丢弃
    assert normalize_emotions("calm") == [EMOTION_DEFAULT, "calm"]  # 单值也接受
    for e in EMOTIONS_ALL:
        assert e in EMOTIONS_ALL
    assert normalize_emotion("") == EMOTION_DEFAULT
    assert normalize_emotion("sad") == "sad"
    assert normalize_emotion("吼叫") == EMOTION_DEFAULT


def test_default_profile_has_default_emotions(store):
    pid = store.create_profile("档位")["id"]
    assert store.get_profile(pid)["emotions"] == list(DEFAULT_EMOTIONS)


def test_set_emotions_persists_and_clamps(store):
    pid = store.create_profile("档位")["id"]
    saved = store.set_emotions(pid, ["happy", "surprised", "非法"])
    assert saved["emotions"] == [EMOTION_DEFAULT, "happy", "surprised"]
    assert store.get_profile(pid)["emotions"] == [EMOTION_DEFAULT, "happy", "surprised"]
    # default 不可取消
    assert store.set_emotions(pid, [])["emotions"] == [EMOTION_DEFAULT]


def test_missing_report_counts_per_emotion(store):
    """缺失口径：无引用 / hash 失配 / 文件不在，都算缺；文本为空的条目不计入。"""
    pid = store.create_profile("缺失")["id"]
    store.add_voice(pid, "voiceA", "a")
    store.set_emotions(pid, ["happy", "calm"])
    f_default = store.save_audio(pid, _wav(), "d.wav")
    f_happy = store.save_audio(pid, _wav(), "h.wav")
    data = store.get_profile(pid)
    data["topics_ai"] = [
        {"id": "t1", "title": "有 default 无 happy", "text": "A",
         "audios": {"voiceA": {"default": {"file": f_default,
                                           "text_hash": text_hash("A")}}},
         "enabled": True},
        {"id": "t2", "title": "文本已改", "text": "新文本",
         "audios": {"voiceA": {"default": {"file": f_happy,
                                           "text_hash": text_hash("旧文本")}}},
         "enabled": True},
        {"id": "t3", "title": "停用", "text": "C", "audios": {}, "enabled": False},
        {"id": "t4", "title": "空文本", "text": "", "audios": {}, "enabled": True},
    ]
    store.save_profile(pid, data)
    report = store.missing_report(pid)
    assert report["voice_id"] == "voiceA"
    assert report["total"] == 2                      # 停用 / 空文本不计
    assert report["missing"] == {"default": 1, "happy": 2, "calm": 2}
    assert store.missing_emotions(pid) == {"default": 1, "happy": 2, "calm": 2}
    # 文件被删也算缺
    store.delete_audio(pid, f_default)
    assert store.missing_emotions(pid)["default"] == 2


def test_count_missing_pure_helper():
    profile = {"current_voice": "v1", "emotions": ["default", "happy"],
               "samples_ai": [{"id": "s1", "reply_text": "hi",
                               "audios": {"v1": {"default": {"file": "a1b2c3.wav",
                                                             "text_hash": text_hash("hi")}}},
                               "enabled": True}]}
    assert count_missing(profile)["missing"] == {"default": 0, "happy": 1}
    assert count_missing(profile, exists=lambda n: False)["missing"] == \
        {"default": 1, "happy": 1}
    assert count_missing({})["missing"] == {}


def test_purge_emotion_removes_files_and_slot(store):
    pid = store.create_profile("清档")["id"]
    store.add_voice(pid, "voiceA", "a")
    store.set_emotions(pid, ["happy"])
    f_default = store.save_audio(pid, _wav(), "d.wav")
    f_happy = store.save_audio(pid, _wav(), "h.wav")
    data = store.get_profile(pid)
    data["topics_ai"].append({"id": "t1", "title": "t", "text": "x",
                              "audios": {"voiceA": {
                                  "default": {"file": f_default,
                                              "text_hash": text_hash("x")},
                                  "happy": {"file": f_happy,
                                            "text_hash": text_hash("x")}}},
                              "enabled": True})
    store.save_profile(pid, data)
    res = store.purge_emotion(pid, "voiceA", "happy")
    assert res["emotion"] == "happy" and res["files"] == 1
    assert res["emotions"] == [EMOTION_DEFAULT]
    assert not store.audio_exists(pid, f_happy)
    assert store.audio_exists(pid, f_default)
    after = store.get_profile(pid)
    assert after["topics_ai"][0]["audios"]["voiceA"] == {
        "default": {"file": f_default, "text_hash": text_hash("x")}}
    with pytest.raises(ScriptStoreError):
        store.purge_emotion(pid, "voiceA", "default")     # default 拒删
    with pytest.raises(ScriptStoreError):
        store.purge_emotion(pid, "voiceA", "吼叫")         # 非法档位


def test_generate_audio_writes_slot_and_registers_emotion(store):
    """generate_audio：合成 → 落盘 → 写 audios[voice][emotion] → 清旧文件 → 自动登记档位。"""
    pid = store.create_profile("合成")["id"]
    store.add_voice(pid, "voiceA", "a")
    data = store.get_profile(pid)
    data["samples_ai"].append({"id": "s1", "type_name": "问下载",
                               "reply_text": "点手柄", "audios": {},
                               "enabled": True})
    store.save_profile(pid, data)
    calls = []

    def fake_synth(text, voice_id, params):
        calls.append((text, voice_id, params))
        return _wav()

    store.set_synthesizer(fake_synth)
    res = store.generate_audio(pid, "s1", "voiceA", "happy")
    assert calls == [("点手柄", "voiceA", {})]
    assert res["kind"] == "sample" and res["emotion"] == "happy"
    assert res["text_hash"] == text_hash("点手柄")
    assert store.audio_exists(pid, res["audio"])
    after = store.get_profile(pid)
    assert after["samples_ai"][0]["audios"]["voiceA"]["happy"]["file"] == res["audio"]
    assert "happy" in after["emotions"]          # 生成即自动登记，避免"生了却用不上"
    assert after["samples_ai"][0]["stale"] == []  # hash 匹配
    # 重复生成：旧文件被清掉，引用指向新文件
    res2 = store.generate_audio(pid, "s1", "voiceA", "happy")
    assert res2["old_audio"] == res["audio"]
    assert not store.audio_exists(pid, res["audio"])
    assert store.audio_exists(pid, res2["audio"])
    # 未注入合成器 → 明确报错（不静默）
    store.set_synthesizer(None)
    with pytest.raises(ScriptStoreError):
        store.generate_audio(pid, "s1", "voiceA", "happy")


def test_generate_audio_rejects_bad_input(store):
    pid = store.create_profile("合成边界")["id"]
    store.set_synthesizer(lambda t, v, p: _wav())
    with pytest.raises(ScriptStoreError):          # 条目不存在
        store.generate_audio(pid, "没有这个id")
    store.add_voice(pid, "voiceA", "a")
    data = store.get_profile(pid)
    data["topics_ai"].append({"id": "t1", "title": "空文本", "text": "",
                              "audios": {}, "enabled": True})
    store.save_profile(pid, data)
    with pytest.raises(ScriptStoreError):          # 文本为空
        store.generate_audio(pid, "t1", "voiceA", "default")
    data = store.get_profile(pid)
    data["topics_ai"][0]["text"] = "正文"
    store.save_profile(pid, data)
    with pytest.raises(ScriptStoreError):          # 音色不在存档内
        store.generate_audio(pid, "t1", "voiceX", "default")
    with pytest.raises(ScriptStoreError):          # 档位非法
        store.generate_audio(pid, "t1", "voiceA", "吼叫")


# ---------- stale 传播（T4） ----------

def test_stale_propagates_to_all_emotion_slots(store):
    """改文本 → 该音色所有档位一起标 stale（同一文本多档音频无法互相派生）。"""
    pid = store.create_profile("stale多档")["id"]
    store.add_voice(pid, "voiceA", "a")
    fa = store.save_audio(pid, _wav(), "d.wav")
    fb = store.save_audio(pid, _wav(), "h.wav")
    data = store.get_profile(pid)
    data["topics_ai"].append({"id": "t1", "title": "t", "text": "原文",
                              "audios": {"voiceA": {
                                  "default": {"file": fa, "text_hash": text_hash("原文")},
                                  "happy": {"file": fb, "text_hash": text_hash("原文")}}},
                              "enabled": True})
    saved = store.save_profile(pid, data)
    assert saved["topics_ai"][0]["stale"] == []
    assert saved["topics_ai"][0]["stale_emotions"] == {}
    saved["topics_ai"][0]["text"] = "改过的话术"
    saved2 = store.save_profile(pid, saved)
    assert saved2["topics_ai"][0]["stale"] == ["voiceA"]
    assert saved2["topics_ai"][0]["stale_emotions"] == {
        "voiceA": ["default", "happy"]}
    # 只补 default 后 happy 仍 stale（voice 级上榜，档位级只剩 happy）
    saved2["topics_ai"][0]["audios"]["voiceA"]["default"]["text_hash"] = \
        text_hash("改过的话术")
    saved3 = store.save_profile(pid, saved2)
    assert saved3["topics_ai"][0]["stale"] == ["voiceA"]
    assert saved3["topics_ai"][0]["stale_emotions"] == {"voiceA": ["happy"]}


# ---------- 首跑种子 ----------

def test_seed_profiles_if_empty(store, tmp_path, monkeypatch):
    import hub.scriptstore as ss

    # M7 起种子选址是「包内 → exe 旁」两级：要验"没种子"必须把两级都指空，
    # 否则会落到真仓库的 assets/seed_profile.zip（源码运行 EXE_DIR == 仓库根）。
    monkeypatch.setattr(ss, "EXE_DIR", tmp_path / "empty_exe_dir")

    # 无种子 zip：不动
    monkeypatch.setattr(ss, "SEED_ZIP", tmp_path / "none.zip")
    assert ss.find_seed_zip() is None
    assert ss.seed_profiles_if_empty(store) == ""
    assert store.list_profiles() == []

    # 有种子 zip：落地为新存档；profiles 非空后不再种
    src = tmp_path / "seed.zip"
    profile = default_profile("示例主播")
    profile["voices"] = [{"voice_id": "v1", "label": "标准", "params": {}}]
    profile["current_voice"] = "v1"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("profile.yaml", yaml.safe_dump(profile, allow_unicode=True))
    monkeypatch.setattr(ss, "SEED_ZIP", src)
    pid = ss.seed_profiles_if_empty(store)
    assert pid and store.exists(pid)
    assert store.get_profile(pid)["name"] == "示例主播"
    assert ss.seed_profiles_if_empty(store) == ""   # 幂等


def test_shipped_seed_zip_is_v4_and_self_consistent():
    """随包种子 zip 必须是 v4 且引用完整——否则新用户首跑落地还是旧结构。

    只读 metadata（profile.yaml + namelist），不解出 70MB 音频，毫秒级。
    重生成种子后若忘记升 version / 漏了音频，这条会立刻红。
    """
    import hub.scriptstore as ss

    if not ss.SEED_ZIP.is_file():
        pytest.skip("本仓库为脱敏公开版，未附带 assets/seed_profile.zip（含真人音色样本），跳过")
    with zipfile.ZipFile(ss.SEED_ZIP) as zf:
        names = set(zf.namelist())
        data = yaml.safe_load(zf.read("profile.yaml").decode("utf-8"))
    assert data.get("version") == ss.PROFILE_VERSION == 4
    assert data.get("emotions") == ["default", "happy", "calm"]

    cur = data.get("current_voice")
    voices = [v.get("voice_id") for v in data.get("voices") or []]
    assert cur in voices and len(voices) >= 2

    refs, per_voice = set(), {}
    for section in ("topics_ai", "samples_ai"):
        for item in data.get(section) or []:
            for vid, slots in (item.get("audios") or {}).items():
                for emo, slot in slots.items():
                    name = (slot or {}).get("file")
                    assert name, f"{item.get('id')}/{vid}/{emo} 引用为空"
                    assert f"audio/{name}" in names, f"{name} 不在 zip 内"
                    refs.add(name)
                    per_voice.setdefault(vid, {}).setdefault(emo, 0)
                    per_voice[vid][emo] += 1
    assert refs, "种子 zip 里没有任何音频引用"
    # 当前音色三档齐全（这是 M2 对种子的核心要求）
    assert set(per_voice[cur]) == {"default", "happy", "calm"}, per_voice[cur]
    # 未引用音频不许夹带（避免孤儿文件把包撑大）
    audio_in_zip = {n[len("audio/"):] for n in names if n.startswith("audio/")}
    assert audio_in_zip == refs, f"孤儿音频 {sorted(audio_in_zip - refs)[:5]}"
