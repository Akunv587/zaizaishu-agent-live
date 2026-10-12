"""hub.voiceworkshop 音色工坊的单元测试（用户需求：Key 配置/上传/复刻/试听/音色库）。

MiniMax 网络调用全部用假客户端工厂替身，不发出真实请求。
"""
import json

import pytest

from hub.voiceworkshop import (VoiceWorkshop, WorkshopError,
                               validate_voice_id, validate_voice_params,
                               _looks_like_audio)


class _FakeClient:
    """替身：按预设返回值记录调用，不发网络请求。"""

    def __init__(self, upload_id=436449258881393, clone_result=None,
                 synthesize_result=None, get_voice_result=None):
        self.upload_id = upload_id
        self.clone_result = clone_result or {"demo_audio": "https://demo/x.mp3"}
        self.synthesize_result = synthesize_result or {
            "data": {"audio": "https://audio/x.mp3"}}
        self.get_voice_result = get_voice_result or {
            "system_voice": [], "voice_cloning": [], "voice_generation": []}
        self.calls = []

    def upload_audio(self, path, purpose):
        self.calls.append(("upload", path, purpose))
        return self.upload_id

    def clone_voice(self, file_id, voice_id, model):
        self.calls.append(("clone", file_id, voice_id, model))
        return self.clone_result

    def synthesize(self, text, voice_id, model, format_):
        self.calls.append(("synthesize", text, voice_id, model, format_))
        return self.synthesize_result

    def get_voice(self, voice_type):
        self.calls.append(("get_voice", voice_type))
        return self.get_voice_result

    def delete_voice(self, voice_id, voice_type):
        self.calls.append(("delete_voice", voice_id, voice_type))
        return {"voice_id": voice_id, "base_resp": {"status_code": 0}}


def _make_workshop(tmp_path, client):
    ws = VoiceWorkshop(data_dir=str(tmp_path), client_factory=lambda k, e: client)
    ws.save_key("sk-test-key", "")      # 工坊各操作前置：已配置 Key
    return ws


# ---------- validate_voice_id ----------

def test_validate_voice_id_rules():
    assert validate_voice_id("zs9x8y7z") is None
    assert validate_voice_id("A-1_bcde") is None
    assert validate_voice_id("") is not None                    # 太短
    assert validate_voice_id("ab") is not None                  # < 8 位
    assert validate_voice_id("1abc5678") is not None            # 首字符非字母
    assert validate_voice_id("abc-5678_") is not None           # 末位下划线
    assert validate_voice_id("abc 5678") is not None            # 含空格
    assert validate_voice_id("abc中文56") is not None            # 含中文


def test_looks_like_audio_magic_numbers():
    assert _looks_like_audio(b"RIFFxxxxWAVE", ".wav")
    assert not _looks_like_audio(b"RIFFxxxxXXXX", ".wav")
    assert _looks_like_audio(b"ID3 tag data", ".mp3")
    assert _looks_like_audio(b"\xff\xfb\x90\x00", ".mp3")
    assert not _looks_like_audio(b"plain text", ".mp3")
    assert _looks_like_audio(b"xxxxftypM4A ", ".m4a")
    assert not _looks_like_audio(b"xxxxxxxxx", ".m4a")


# ---------- Key 配置 ----------

def test_minimax_status_and_save_key(tmp_path):
    ws = VoiceWorkshop(data_dir=str(tmp_path), client_factory=lambda k, e: _FakeClient())
    assert ws.minimax_status() == {"configured": False, "endpoint": "https://api.minimaxi.com",
                                    "masked_key": ""}
    ws.save_key("sk-minimax-key-123456", "")
    status = ws.minimax_status()
    assert status["configured"] is True
    assert status["masked_key"].startswith("sk-min")
    assert "****" in status["masked_key"]
    assert status["endpoint"] == "https://api.minimaxi.com"     # 空端点回落默认值
    # 写入的是 config.json（音色工坊数据目录）
    data = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert data["api_key"] == "sk-minimax-key-123456"


def test_save_key_rejects_empty(tmp_path):
    ws = _make_workshop(tmp_path, _FakeClient())
    with pytest.raises(WorkshopError):
        ws.save_key("  ", "https://api.minimaxi.com")


def test_probe_key_ok_and_error(tmp_path):
    client = _FakeClient()
    ws = _make_workshop(tmp_path, client)
    ws.save_key("sk-k", "")
    assert ws.probe_key("", "") is None                        # 留空回落现有 Key
    assert client.calls and client.calls[0][0] == "synthesize"

    import minimax_client as _vc

    class _ApiErrClient(_FakeClient):
        def synthesize(self, *a, **kw):
            raise _vc.MiniMaxError(1004, "invalid api key")

    ws2 = VoiceWorkshop(data_dir=str(tmp_path), client_factory=lambda k, e: _ApiErrClient())
    ws2.save_key("sk-k", "")
    err = ws2.probe_key("", "")
    assert err and "invalid api key" in err


# ---------- 上传 ----------

def _wav_bytes(size=4096):
    # RIFF 标头 + 4 字节长度 + WAVE 魔数（[0:4]=RIFF, [8:12]=WAVE）
    return b"RIFF" + b"\x00\x00\x10\x00" + b"WAVE" + b"0" * (size - 12)


def test_upload_audio_success(tmp_path):
    client = _FakeClient()
    ws = _make_workshop(tmp_path, client)
    rec = ws.upload_audio(_wav_bytes(), "我的声音.wav")
    assert rec["file_id"] == 436449258881393
    assert rec["name"] == "我的声音.wav"
    assert ws.voices()[-1] == rec                       # 记入音色库
    assert client.calls[0][0] == "upload"
    assert (tmp_path / "uploads").exists()              # 本地留档
    assert list((tmp_path / "uploads").iterdir())       # 落盘了文件


def test_upload_audio_validates(tmp_path):
    ws = _make_workshop(tmp_path, _FakeClient())
    with pytest.raises(WorkshopError):                   # 扩展名不支持
        ws.upload_audio(_wav_bytes(), "a.txt")
    with pytest.raises(WorkshopError):                   # 空内容
        ws.upload_audio(b"", "a.wav")
    with pytest.raises(WorkshopError):                   # 太小（占位/空录音）
        ws.upload_audio(b"RI", "a.wav")
    with pytest.raises(WorkshopError):                   # 魔数不符
        ws.upload_audio(b"0" * 4096, "a.wav")
    assert ws.voices() == []


def test_upload_audio_rejects_oversize(tmp_path):
    ws = _make_workshop(tmp_path, _FakeClient())
    big = _wav_bytes(4096) + b"0" * (20 * 1024 * 1024)
    with pytest.raises(WorkshopError):
        ws.upload_audio(big, "a.wav")


# ---------- 复刻 ----------

def test_clone_voice_success(tmp_path):
    client = _FakeClient()
    ws = _make_workshop(tmp_path, client)
    ws.upload_audio(_wav_bytes(), "src.wav")
    rec = ws.clone_voice(436449258881393, "zsVoice0001", voice_name="主播小美")
    assert rec["voice_id"] == "zsVoice0001"
    assert rec["voice_name"] == "主播小美"
    assert rec["demo_audio"] == "https://demo/x.mp3"
    assert rec["last_called"] is None
    assert client.calls[-1] == ("clone", 436449258881393, "zsVoice0001", "speech-2.8-hd")
    assert any(r.get("voice_id") == "zsVoice0001" for r in ws.voices())


def test_clone_voice_validates_voice_id(tmp_path):
    ws = _make_workshop(tmp_path, _FakeClient())
    for bad in ("", "ab", "1abc5678", "abc-5678_"):
        with pytest.raises(WorkshopError):
            ws.clone_voice(123, bad)


# ---------- 合成试听 ----------

def test_preview_success_touches_keepalive(tmp_path):
    client = _FakeClient()
    ws = _make_workshop(tmp_path, client)
    ws.clone_voice(436449258881393, "zsVoice0001")
    res = ws.preview("家人们晚上好", "zsVoice0001")
    assert res["audio_url"] == "https://audio/x.mp3"
    assert res["chars"] == 6
    lib = ws.voices()
    rec = next(r for r in lib if r.get("voice_id") == "zsVoice0001")
    assert rec["last_called"]                            # 正式合成记入 7 天保活


def test_preview_validates_input(tmp_path):
    ws = _make_workshop(tmp_path, _FakeClient())
    with pytest.raises(WorkshopError):
        ws.preview("  ", "zsVoice0001")
    with pytest.raises(WorkshopError):
        ws.preview("你好", "")


# ---------- 音色库 ----------

def test_voices_empty_and_corrupt_file(tmp_path):
    ws = _make_workshop(tmp_path, _FakeClient())
    assert ws.voices() == []
    (tmp_path / "voice_library.json").write_text("not json", encoding="utf-8")
    assert ws.voices() == []                               # 损坏不炸，按空处理


# ---------- 参数校验（语速/音量/音调/情绪） ----------

def test_validate_voice_params_defaults():
    """空入参回落到 MiniMax 默认（1.0 / 1.0 / 0 / 自动情绪）。"""
    assert validate_voice_params({}) == {"speed": 1.0, "vol": 1.0,
                                          "pitch": 0, "emotion": ""}
    assert validate_voice_params(None) == {"speed": 1.0, "vol": 1.0,
                                            "pitch": 0, "emotion": ""}


def test_validate_voice_params_normalizes():
    p = validate_voice_params({"speed": "1.234", "vol": 2.567,
                               "pitch": 1.6, "emotion": "happy"})
    assert p == {"speed": 1.23, "vol": 2.6, "pitch": 2, "emotion": "happy"}


def test_validate_voice_params_rejects_out_of_range():
    with pytest.raises(WorkshopError):
        validate_voice_params({"speed": 0.4})      # < 0.5
    with pytest.raises(WorkshopError):
        validate_voice_params({"speed": 2.1})      # > 2.0
    with pytest.raises(WorkshopError):
        validate_voice_params({"vol": 0})          # 必须 > 0
    with pytest.raises(WorkshopError):
        validate_voice_params({"vol": 11})         # > 10
    with pytest.raises(WorkshopError):
        validate_voice_params({"pitch": -13})
    with pytest.raises(WorkshopError):
        validate_voice_params({"pitch": 13})


def test_validate_voice_params_rejects_bad_type():
    with pytest.raises(WorkshopError):
        validate_voice_params({"speed": "abc"})
    with pytest.raises(WorkshopError):
        validate_voice_params({"vol": None})


def test_validate_voice_params_emotion():
    assert validate_voice_params({"emotion": "fluent"})["emotion"] == "fluent"
    assert validate_voice_params({"emotion": ""})["emotion"] == ""
    with pytest.raises(WorkshopError):
        validate_voice_params({"emotion": "excited"})     # 不在官方枚举里


# ---------- 账号音色拉取（一键获取） ----------

def test_fetch_account_voices_merges_new(tmp_path):
    client = _FakeClient(get_voice_result={
        "system_voice": [{"voice_id": "male-qn-qingse"},
                          {"voice_id": "female-shaonv"}],
        "voice_cloning": [{"voice_id": "zsVoiceA", "created_time": "2026-09-01 10:00:00"}],
        "voice_generation": [{"voice_id": "zsVoiceB", "created_time": ""}],
    })
    ws = _make_workshop(tmp_path, client)
    # 库里先有一个已存在的 zsVoiceA：不应重复导入
    ws.clone_voice(436449258881393, "zsVoiceA")
    res = ws.fetch_account_voices()
    assert client.calls[-1] == ("get_voice", "all")
    # 只新增 zsVoiceB（zsVoiceA 已在库）
    assert len(res["added"]) == 1
    assert res["added"][0]["voice_id"] == "zsVoiceB"
    assert res["added"][0]["source"] == "account_fetch"
    assert res["system_count"] == 2
    ids = [r.get("voice_id") for r in ws.voices() if r.get("voice_id")]
    assert ids.count("zsVoiceA") == 1 and ids.count("zsVoiceB") == 1


def test_fetch_account_voices_idempotent(tmp_path):
    """再拉一次：全部已存在，返回 added=0，不改库。"""
    client = _FakeClient(get_voice_result={
        "system_voice": [],
        "voice_cloning": [{"voice_id": "zsVoiceA", "created_time": ""}],
        "voice_generation": [],
    })
    ws = _make_workshop(tmp_path, client)
    res1 = ws.fetch_account_voices()
    assert len(res1["added"]) == 1
    res2 = ws.fetch_account_voices()
    assert res2["added"] == []
    assert len([r for r in ws.voices() if r.get("voice_id") == "zsVoiceA"]) == 1


def test_fetch_account_voices_api_error(tmp_path):
    import minimax_client as _vc

    class _ErrClient(_FakeClient):
        def get_voice(self, voice_type):
            raise _vc.MiniMaxError(1004, "invalid api key")

    ws = _make_workshop(tmp_path, _ErrClient())
    with pytest.raises(WorkshopError):
        ws.fetch_account_voices()


# ---------- 音色参数档案 ----------

def test_voice_params_roundtrip(tmp_path):
    ws = _make_workshop(tmp_path, _FakeClient())
    ws.clone_voice(436449258881393, "zsVoice0001")
    assert ws.voice_params("zsVoice0001") is None          # 未设置
    rec = ws.set_voice_params("zsVoice0001",
                              {"speed": 1.2, "vol": 3.5, "pitch": -2,
                               "emotion": "happy"})
    assert rec["params"] == {"speed": 1.2, "vol": 3.5, "pitch": -2,
                              "emotion": "happy"}
    # 持久化到 voice_library.json
    ws2 = _make_workshop(tmp_path, _FakeClient())
    assert ws2.voice_params("zsVoice0001") == rec["params"]


def test_voice_params_unknown_voice(tmp_path):
    ws = _make_workshop(tmp_path, _FakeClient())
    assert ws.voice_params("no-such-voice") is None
    with pytest.raises(WorkshopError):
        ws.set_voice_params("no-such-voice", {"speed": 1.0})


def test_set_voice_params_validates(tmp_path):
    ws = _make_workshop(tmp_path, _FakeClient())
    ws.clone_voice(436449258881393, "zsVoice0001")
    with pytest.raises(WorkshopError):
        ws.set_voice_params("zsVoice0001", {"speed": 99})
    # 校验失败时不应落盘
    assert ws.voice_params("zsVoice0001") is None


# ---------- 删除账号音色（平台侧真实删除） ----------

def test_delete_voice_success(tmp_path):
    """删除成功：调平台接口（按记录 voice_type）+ 移出本地库 + 落盘。"""
    client = _FakeClient()
    ws = _make_workshop(tmp_path, client)
    ws.clone_voice(436449258881393, "zsVoice0001")
    res = ws.delete_voice("zsVoice0001", live_voice_id="otherVoice")
    assert ("delete_voice", "zsVoice0001", "voice_cloning") in client.calls
    assert res["voice_id"] == "zsVoice0001"
    assert all(it.get("voice_id") != "zsVoice0001" for it in ws.voices())
    # 重新加载确认已持久化
    ws2 = _make_workshop(tmp_path, client)
    assert all(it.get("voice_id") != "zsVoice0001" for it in ws2.voices())


def test_delete_voice_blocks_live_voice(tmp_path):
    """直播音色禁删：相同 voice_id 直接拒绝，不调平台接口。"""
    client = _FakeClient()
    ws = _make_workshop(tmp_path, client)
    ws.clone_voice(436449258881393, "zsVoice0001")
    with pytest.raises(WorkshopError, match="直播音色"):
        ws.delete_voice("zsVoice0001", live_voice_id="zsVoice0001")
    assert not any(c[0] == "delete_voice" for c in client.calls)
    assert any(it.get("voice_id") == "zsVoice0001" for it in ws.voices())


def test_delete_voice_unknown(tmp_path):
    ws = _make_workshop(tmp_path, _FakeClient())
    with pytest.raises(WorkshopError, match="不存在"):
        ws.delete_voice("no-such-voice")


def test_delete_voice_uses_recorded_type(tmp_path):
    """文生音色按 voice_generation 类型删除（拉取时记录的类型）。"""
    client = _FakeClient(get_voice_result={
        "system_voice": [],
        "voice_cloning": [],
        "voice_generation": [{"voice_id": "genVoice1", "created_time": ""}],
    })
    ws = _make_workshop(tmp_path, client)
    ws.fetch_account_voices()
    ws.delete_voice("genVoice1")
    assert ("delete_voice", "genVoice1", "voice_generation") in client.calls


def test_fetch_backfills_voice_type(tmp_path):
    """老库记录无 voice_type：再次拉取时按分组回填并落盘。"""
    client = _FakeClient(get_voice_result={
        "system_voice": [],
        "voice_cloning": [{"voice_id": "zsVoice0001", "created_time": ""}],
        "voice_generation": [],
    })
    ws = _make_workshop(tmp_path, client)
    ws.clone_voice(436449258881393, "zsVoice0001")
    # 模拟老版本记录：去掉 voice_type
    lib = ws.voices()
    for it in lib:
        it.pop("voice_type", None)
    ws._save_library(lib)
    ws.fetch_account_voices()
    rec = next(it for it in ws.voices() if it.get("voice_id") == "zsVoice0001")
    assert rec["voice_type"] == "voice_cloning"
    # 已落盘：重载后仍在
    ws2 = _make_workshop(tmp_path, client)
    rec2 = next(it for it in ws2.voices() if it.get("voice_id") == "zsVoice0001")
    assert rec2["voice_type"] == "voice_cloning"
