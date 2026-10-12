"""webui 话术管理动作族（v4：单方案纯AI + 存档内多音色 × 多情绪档）+ Cookie 卡的单元测试。

验收口径：
- sc_list/sc_get/sc_save/sc_create/sc_rename/sc_delete 全链路广播
- sc_set_active：写回 config.yaml 指针 + 运行时热应用回调触发
- sc_delete 激活存档：兜底切换并热应用
- 存档音色动作族：sc_voice_add（校验工坊库）/sc_voice_update（参数校验）/
  sc_set_voice（热应用）/sc_voice_remove（连带删音频，当前音色拒删）
- 分片上传：音频直挂 audios[voice][emotion]（magic 校验 + 音色须在存档内）/ zip 导入广播 warnings
- sc_gen_audio：单条×单音色×单情绪档合成 → audios[voice][emotion] 更新 + 旧音频清理
- sc_gen_all：批量补齐缺失/过期音频（音色 × 档位），进度广播 + 失败收集 + 取消
- sc_emotion_list / sc_emotion_save / sc_emotion_gen / sc_emotion_purge：情绪档位区
- cookie_get / cookie_set：状态查询 + configurer 转交与失败可读化
- HTTP：/sc_export zip 下载、/sc_audio 音频直链
"""
import asyncio
import io
import json
import time
import wave
import zipfile

import pytest
import yaml

from hub.scriptstore import ScriptStore, text_hash
from hub.selector import ScriptRuntime
from hub.stats import StatsCollector
from hub.webui import WebUi


@pytest.fixture
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


def _wav_bytes(frames=4800) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(b"\x01\x00" * frames)
    return buf.getvalue()


class _FakeWorkshop:
    """假音色工坊：synthesize_wav 记录 (text, voice_id, params)；on_synth 钩子供取消测试。"""

    def __init__(self, known_voices=()):
        self.synth_calls = []
        self._voices = [{"voice_id": v, "voice_name": v} for v in known_voices]
        self.on_synth = None

    def synthesize_wav(self, text, voice_id, model=None, params=None):
        self.synth_calls.append((text, voice_id, dict(params or {})))
        if self.on_synth is not None:
            self.on_synth()
        if voice_id == "bad_voice":
            from hub.voiceworkshop import WorkshopError
            raise WorkshopError("合成失败：音色不存在")
        return _wav_bytes()

    def minimax_status(self):
        return {"configured": True, "endpoint": "https://x", "masked_key": "sk-1****"}

    def voices(self):
        return list(self._voices)


def _make_ui(tmp_path, cfg=None, workshop=None):
    base_cfg = {"webui": {"enabled": False, "listen": "127.0.0.1:0"},
                "danmaku": {"cookie": ""},
                "script": {"active_profile": ""},
                "_config_path": str(tmp_path / "config.yaml")}
    (tmp_path / "config.yaml").write_text(
        "script:\n  active_profile: \"\"\n", encoding="utf-8")
    if cfg:
        base_cfg.update(cfg)
    ui = WebUi(config=base_cfg, stats=StatsCollector(),
               voice_workshop=workshop or _FakeWorkshop())
    store = ScriptStore(root=tmp_path / "profiles")
    rt = ScriptRuntime(store, base_cfg, stats=StatsCollector())
    defaulted = rt.reload()
    if defaulted:
        base_cfg["script"]["active_profile"] = defaulted
    applied = []
    ui.script_store = store
    ui.script_runtime = rt
    ui.script_applier = lambda: applied.append(1)
    sent = []
    ui._broadcast = lambda msg: sent.append(msg)
    return ui, store, rt, applied, sent


def _script_msgs(sent, op=None):
    out = [m["data"] for m in sent if m.get("type") == "script"]
    if op is not None:
        out = [d for d in out if d.get("op") == op]
    return out


def _act(ui, payload, vc_upload=None):
    asyncio.run(ui._handle_action(json.dumps(payload),
                                  vc_upload if vc_upload is not None else {}))


async def _quiesce(timeout=10.0):
    """等**本轮派生的后台任务**全部收尾——替代固定 sleep 的通用做法。

    为什么必须这样：这些动作（批量生成 / 单条生成 / 试听）都经
    `asyncio.create_task` + `asyncio.to_thread` 落到后台（真实线程 + 文件 IO），
    耗时随机器负载浮动。固定 `sleep(0.2)` 在负载高时会**漏掉尾部帧**，表现为
    「断言读到的还是上一轮的消息」或 `IndexError: list index out of range`。
    每个用例都由 `asyncio.run(_act_async(...))` 起独立循环，所以此刻 `all_tasks()`
    里只剩被调用方与它派生的任务——空即收尾，语义精确且与负载无关。
    """
    cur = asyncio.current_task()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not [t for t in asyncio.all_tasks() if t is not cur and not t.done()]:
            await asyncio.sleep(0)      # 让收尾的同步广播落地
            return
        await asyncio.sleep(0.01)


async def _wait_gen_idle(ui, timeout=10.0):
    """等批量生成真正收尾（`ui._gen_running` 清空）——`_quiesce` 之外的显式标记。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not ui._gen_running:
            return
        await asyncio.sleep(0.01)


async def _act_async(ui, payload, vc_upload=None, wait_gen=True):
    """调用 action 并等待其派生的后台任务收尾。"""
    await ui._handle_action(json.dumps(payload),
                            vc_upload if vc_upload is not None else {})
    await _quiesce()
    if wait_gen:
        await _wait_gen_idle(ui)
        await _quiesce()


def _seed_items(store, pid, emotions=("default",)):
    """给存档铺一条话题 + 一条样本（无音频），档位集合默认只留 default（算术简单）。

    返回存档数据。
    """
    p = store.get_profile(pid)
    p["topics_ai"] = [{"id": "t1", "title": "话题一", "text": "这游戏点点小手柄就能下载",
                       "audios": {}, "enabled": True}]
    p["samples_ai"] = [{"id": "s1", "type_name": "问下载", "examples": ["怎么下载"],
                        "reply_text": "点小手柄就好啦", "audios": {}, "enabled": True}]
    p["emotions"] = ["default"] if list(emotions) == ["default"] else list(emotions)
    store.save_profile(pid, p)
    return p


# ---------- 存档 CRUD ----------

def test_sc_list_and_get(logger_setup, tmp_path):
    ui, store, rt, applied, sent = _make_ui(tmp_path)
    _act(ui, {"action": "sc_list"})
    lst = _script_msgs(sent, "list")[-1]
    assert lst["active"] == rt.profile_id
    assert any(p["id"] == rt.profile_id for p in lst["profiles"])

    _act(ui, {"action": "sc_get", "profile_id": rt.profile_id})
    prof = _script_msgs(sent, "profile")[-1]["profile"]
    assert prof["id"] == rt.profile_id
    assert prof["knowledge"]["nickname"] == "仔仔鼠"
    assert prof["voices"] == [] and prof["current_voice"] == ""

    _act(ui, {"action": "sc_get", "profile_id": "p_none"})
    assert "不存在" in _script_msgs(sent, "error")[-1]["error"]


def test_sc_save_applies_hot_when_active(logger_setup, tmp_path):
    ui, store, rt, applied, sent = _make_ui(tmp_path)
    profile = store.get_profile(rt.profile_id)
    profile["knowledge"]["game_name"] = "新游戏"
    asyncio.run(_act_async(ui, {"action": "sc_save", "profile_id": rt.profile_id,
                                "profile": profile}))
    saved = _script_msgs(sent, "saved")[-1]["profile"]
    assert saved["knowledge"]["game_name"] == "新游戏"
    assert len(applied) == 1                        # 激活存档保存 → 热应用
    assert rt.profile["knowledge"]["game_name"] == "新游戏"

    # 非激活存档保存不触发热应用
    other = store.create_profile("另一个")
    asyncio.run(_act_async(ui, {"action": "sc_save", "profile_id": other["id"],
                                "profile": other}))
    assert len(applied) == 1


def test_sc_create_rename_delete(logger_setup, tmp_path):
    ui, store, rt, applied, sent = _make_ui(tmp_path)
    _act(ui, {"action": "sc_create", "name": "游戏推广风"})
    created = _script_msgs(sent, "created")[-1]["profile"]
    assert created["name"] == "游戏推广风"

    _act(ui, {"action": "sc_rename", "profile_id": created["id"], "name": "改名后"})
    assert _script_msgs(sent, "renamed")[-1]["name"] == "改名后"

    _act(ui, {"action": "sc_delete", "profile_id": created["id"]})
    assert _script_msgs(sent, "deleted")[-1]["id"] == created["id"]
    assert not store.exists(created["id"])
    assert len(applied) == 0                        # 非激活存档删除不热应用


def test_sc_delete_active_falls_back(logger_setup, tmp_path):
    ui, store, rt, applied, sent = _make_ui(tmp_path)
    active = rt.profile_id
    store.create_profile("备档")
    _act(ui, {"action": "sc_delete", "profile_id": active})
    assert not store.exists(active)
    assert len(applied) == 1                        # 激活被删 → 兜底 + 热应用
    assert rt.profile_id != active
    assert ui._config["script"]["active_profile"] == rt.profile_id
    notes = [e["text"] for e in ui._feed if e["kind"] == "system"]
    assert any("已自动切换" in t for t in notes)


def test_sc_set_active(logger_setup, tmp_path):
    ui, store, rt, applied, sent = _make_ui(tmp_path)
    other = store.create_profile("第二存档")
    _act(ui, {"action": "sc_set_active", "profile_id": other["id"]})
    assert rt.profile_id == other["id"]
    assert len(applied) == 1
    cfg_on_disk = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert cfg_on_disk["script"]["active_profile"] == other["id"]

    _act(ui, {"action": "sc_set_active", "profile_id": "p_none"})
    assert "不存在" in _script_msgs(sent, "error")[-1]["error"]


# ---------- 存档音色动作族 ----------

def test_sc_voice_add_and_set_current(logger_setup, tmp_path):
    ui, store, rt, applied, sent = _make_ui(
        tmp_path, workshop=_FakeWorkshop(known_voices=("v1", "v2")))
    pid = rt.profile_id

    _act(ui, {"action": "sc_voice_add", "profile_id": pid, "voice_id": "v1",
              "label": "标准"})
    added = _script_msgs(sent, "voice_added")[-1]
    assert added["voice_id"] == "v1"
    assert added["profile"]["current_voice"] == "v1"     # 首个音色自动成为当前
    assert added["profile"]["voices"][0]["label"] == "标准"
    assert len(applied) == 1                             # 激活存档 → 热应用

    # 未给 label 时回退音色工坊里的名字
    _act(ui, {"action": "sc_voice_add", "profile_id": pid, "voice_id": "v2"})
    assert _script_msgs(sent, "voice_added")[-1]["profile"]["voices"][1]["label"] == "v2"

    # 不在音色工坊库里的音色拒绝挂入
    _act(ui, {"action": "sc_voice_add", "profile_id": pid, "voice_id": "ghost"})
    assert "不在音色工坊库中" in _script_msgs(sent, "error")[-1]["error"]

    # 切当前直播音色 → 热应用 + voice_set 广播
    _act(ui, {"action": "sc_set_voice", "profile_id": pid, "voice_id": "v2"})
    vst = _script_msgs(sent, "voice_set")[-1]
    assert vst["voice_id"] == "v2"
    assert store.get_profile(pid)["current_voice"] == "v2"
    assert len(applied) == 3

    # 切到不在存档内的音色 → 可读错误
    _act(ui, {"action": "sc_set_voice", "profile_id": pid, "voice_id": "ghost"})
    assert "不在存档音色列表中" in _script_msgs(sent, "error")[-1]["error"]


def test_sc_voice_update_label_and_params(logger_setup, tmp_path):
    ui, store, rt, applied, sent = _make_ui(tmp_path)
    pid = rt.profile_id
    store.add_voice(pid, "v1", "标准")

    _act(ui, {"action": "sc_voice_update", "profile_id": pid, "voice_id": "v1",
              "label": "标准版", "params": {"speed": 1.2, "emotion": "happy"}})
    upd = _script_msgs(sent, "voice_updated")[-1]
    voice = upd["profile"]["voices"][0]
    assert voice["label"] == "标准版"
    assert voice["params"]["speed"] == 1.2 and voice["params"]["emotion"] == "happy"
    assert len(applied) == 1                             # 激活存档 → 热应用

    # 参数越界 → 校验拒绝，原参数不动
    _act(ui, {"action": "sc_voice_update", "profile_id": pid, "voice_id": "v1",
              "params": {"speed": 9.9}})
    assert "语速" in _script_msgs(sent, "error")[-1]["error"]
    assert store.get_profile(pid)["voices"][0]["params"]["speed"] == 1.2


def test_sc_voice_remove_deletes_audio_and_guards_current(logger_setup, tmp_path):
    ui, store, rt, applied, sent = _make_ui(tmp_path)
    pid = rt.profile_id
    _seed_items(store, pid)
    store.add_voice(pid, "v1", "标准")
    store.add_voice(pid, "v2", "情绪")
    # 手工挂一条 v2 音频（验证移除音色连带删文件）
    fname = store.save_audio(pid, _wav_bytes(), "a.wav")
    p = store.get_profile(pid)
    p["topics_ai"][0]["audios"]["v2"] = {"file": fname,
                                         "text_hash": text_hash("这游戏点点小手柄就能下载")}
    store.save_profile(pid, p)

    # 当前直播音色（v1）拒删
    _act(ui, {"action": "sc_voice_remove", "profile_id": pid, "voice_id": "v1"})
    assert "当前直播音色" in _script_msgs(sent, "error")[-1]["error"]

    # 移除 v2：音色出列表 + 其音频引用与文件一并清除
    _act(ui, {"action": "sc_voice_remove", "profile_id": pid, "voice_id": "v2"})
    rem = _script_msgs(sent, "voice_removed")[-1]
    assert [v["voice_id"] for v in rem["profile"]["voices"]] == ["v1"]
    assert "v2" not in store.get_profile(pid)["topics_ai"][0]["audios"]
    assert not store.audio_exists(pid, fname)
    assert len(applied) == 1


# ---------- 分片上传：音频 / zip 导入 ----------

def test_sc_audio_upload_attaches_to_voice(logger_setup, tmp_path):
    ui, store, rt, applied, sent = _make_ui(tmp_path)
    pid = rt.profile_id
    _seed_items(store, pid)
    store.add_voice(pid, "v1", "标准")

    session = {}
    wav = _wav_bytes()
    _act(ui, {"action": "sc_upload", "target": "script_audio", "name": "reply.wav",
              "profile_id": pid, "kind": "sample", "item_id": "s1",
              "voice_id": "v1"}, session)
    ui._vc_chunk(wav[:3000], session)
    ui._vc_chunk(wav[3000:], session)
    asyncio.run(_act_async(ui, {"action": "sc_upload_end"}, session))
    msg = _script_msgs(sent, "audio_generated")[-1]
    assert msg["profile_id"] == pid and msg["voice_id"] == "v1"
    assert msg["text_hash"] == text_hash("点小手柄就好啦")
    assert store.audio_exists(pid, msg["audio"])
    item = store.get_profile(pid)["samples_ai"][0]
    assert item["audios"]["v1"]["default"]["file"] == msg["audio"]
    assert msg["emotion"] == "default"
    assert len(applied) == 1                        # 激活存档音频入库 → 热应用

    # 再传一次：旧文件被替换清理
    first = msg["audio"]
    session2 = {}
    _act(ui, {"action": "sc_upload", "target": "script_audio", "name": "reply2.wav",
              "profile_id": pid, "kind": "sample", "item_id": "s1",
              "voice_id": "v1"}, session2)
    ui._vc_chunk(wav, session2)
    asyncio.run(_act_async(ui, {"action": "sc_upload_end"}, session2))
    msg2 = _script_msgs(sent, "audio_generated")[-1]
    assert msg2["audio"] != first
    assert not store.audio_exists(pid, first)

    # 清除该音频文件
    _act(ui, {"action": "sc_audio_delete", "profile_id": pid, "name": msg2["audio"]})
    assert _script_msgs(sent, "audio_deleted")[-1]["audio"] == msg2["audio"]
    assert not store.audio_exists(pid, msg2["audio"])


def test_sc_audio_upload_rejects_voice_outside_profile(logger_setup, tmp_path):
    ui, store, rt, applied, sent = _make_ui(tmp_path)
    pid = rt.profile_id
    _seed_items(store, pid)
    session = {}
    _act(ui, {"action": "sc_upload", "target": "script_audio", "name": "a.wav",
              "profile_id": pid, "kind": "sample", "item_id": "s1",
              "voice_id": "ghost"}, session)
    ui._vc_chunk(_wav_bytes(), session)
    asyncio.run(_act_async(ui, {"action": "sc_upload_end"}, session))
    assert "不在存档音色列表中" in _script_msgs(sent, "error")[-1]["error"]


def test_sc_import_zip(logger_setup, tmp_path):
    ui, store, rt, applied, sent = _make_ui(tmp_path)
    # 造一个合法 v3 zip：profile.yaml（voices + audios 引用）+ 一个音频
    profile = store.get_profile(rt.profile_id)
    profile["name"] = "导入档"
    profile["voices"] = [{"voice_id": "v1", "label": "标准", "params": {}}]
    profile["current_voice"] = "v1"
    profile["topics_ai"] = [{"id": "t_1", "title": "话题A", "text": "文本",
                             "audios": {"v1": {"file": "aa11bb22cc33.wav",
                                               "text_hash": text_hash("文本")}},
                             "enabled": True}]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("profile.yaml", yaml.safe_dump(
            {k: v for k, v in profile.items() if k != "id"}, allow_unicode=True))
        zf.writestr("audio/aa11bb22cc33.wav", _wav_bytes())
        zf.writestr("audio/evil../hack.wav", b"RIFF")   # 非法条目应进 warnings
    session = {}
    _act(ui, {"action": "sc_upload", "target": "script_import", "name": "x.zip"}, session)
    ui._vc_chunk(buf.getvalue(), session)
    asyncio.run(_act_async(ui, {"action": "sc_upload_end"}, session))
    done = _script_msgs(sent, "import_done")[-1]
    assert done["profile"]["name"] == "导入档"
    assert done["profile"]["current_voice"] == "v1"
    assert store.audio_exists(done["profile"]["id"], "aa11bb22cc33.wav")
    assert any("非法音频条目" in w for w in done["warnings"])


# ---------- 条目音频生成（sc_gen_audio / sc_gen_all） ----------

def test_sc_gen_audio_topic_and_sample(logger_setup, tmp_path):
    ui, store, rt, applied, sent = _make_ui(tmp_path)
    pid = rt.profile_id
    _seed_items(store, pid)
    store.add_voice(pid, "myVoice001", "标准")
    store.add_voice(pid, "bad_voice", "坏的")

    # 话题：合成 → audio_generated 广播 + audios[音色][default] 引用更新
    asyncio.run(_act_async(ui, {"action": "sc_gen_audio", "profile_id": pid,
                                "kind": "topic", "item_id": "t1",
                                "voice_id": "myVoice001"}))
    msg = _script_msgs(sent, "audio_generated")[-1]
    assert msg["kind"] == "topic" and msg["item_id"] == "t1"
    assert msg["voice_id"] == "myVoice001"
    assert msg["emotion"] == "default"          # 缺省档位 = default
    assert msg["text_hash"] == text_hash("这游戏点点小手柄就能下载")
    topic = store.get_profile(pid)["topics_ai"][0]
    assert topic["audios"]["myVoice001"]["default"]["file"] == msg["audio"]
    assert store.audio_exists(pid, msg["audio"])
    assert len(applied) == 1
    first_audio = msg["audio"]

    # 指定情绪档：写进 audios[音色][happy]，且该档自动登记进档位集合
    asyncio.run(_act_async(ui, {"action": "sc_gen_audio", "profile_id": pid,
                                "kind": "topic", "item_id": "t1",
                                "voice_id": "myVoice001", "emotion": "happy"}))
    hm = _script_msgs(sent, "audio_generated")[-1]
    assert hm["emotion"] == "happy" and hm["audio"] != first_audio
    after_happy = store.get_profile(pid)
    assert after_happy["topics_ai"][0]["audios"]["myVoice001"]["happy"]["file"] == hm["audio"]
    assert "happy" in after_happy["emotions"]
    # 非法档位字符串归一为 default（不抛错）
    asyncio.run(_act_async(ui, {"action": "sc_gen_audio", "profile_id": pid,
                                "kind": "topic", "item_id": "t1",
                                "voice_id": "myVoice001", "emotion": "吼叫"}))
    assert _script_msgs(sent, "audio_generated")[-1]["emotion"] == "default"

    # 样本：缺省 voice_id = 存档当前音色（myVoice001）
    asyncio.run(_act_async(ui, {"action": "sc_gen_audio", "profile_id": pid,
                                "kind": "sample", "item_id": "s1"}))
    msg2 = _script_msgs(sent, "audio_generated")[-1]
    assert msg2["kind"] == "sample" and msg2["voice_id"] == "myVoice001"
    sample = store.get_profile(pid)["samples_ai"][0]
    assert sample["audios"]["myVoice001"]["default"]["file"] == msg2["audio"]

    # 再生成话题×同音色×default：旧音频文件被清理（引用替换）
    prev_default = store.get_profile(pid)["topics_ai"][0]["audios"]["myVoice001"]["default"]["file"]
    asyncio.run(_act_async(ui, {"action": "sc_gen_audio", "profile_id": pid,
                                "kind": "topic", "item_id": "t1",
                                "voice_id": "myVoice001"}))
    msg3 = _script_msgs(sent, "audio_generated")[-1]
    assert msg3["audio"] != prev_default
    assert not store.audio_exists(pid, prev_default)

    # 合成失败：可读错误广播，条目引用不被破坏
    asyncio.run(_act_async(ui, {"action": "sc_gen_audio", "profile_id": pid,
                                "kind": "topic", "item_id": "t1",
                                "voice_id": "bad_voice"}))
    err = _script_msgs(sent, "error")[-1]
    assert err["stage"] == "gen_audio" and "合成失败" in err["error"]
    assert (store.get_profile(pid)["topics_ai"][0]
            ["audios"]["myVoice001"]["default"]["file"] == msg3["audio"])

    # 未知类型 / 条目不存在 / 音色不在存档：可读错误
    asyncio.run(_act_async(ui, {"action": "sc_gen_audio", "profile_id": pid,
                                "kind": "weird", "item_id": "t1",
                                "voice_id": "myVoice001"}))
    assert "未知条目类型" in _script_msgs(sent, "error")[-1]["error"]
    asyncio.run(_act_async(ui, {"action": "sc_gen_audio", "profile_id": pid,
                                "kind": "sample", "item_id": "ghost",
                                "voice_id": "myVoice001"}))
    assert "条目不存在" in _script_msgs(sent, "error")[-1]["error"]
    asyncio.run(_act_async(ui, {"action": "sc_gen_audio", "profile_id": pid,
                                "kind": "sample", "item_id": "s1",
                                "voice_id": "ghost"}))
    assert "不在存档音色列表中" in _script_msgs(sent, "error")[-1]["error"]


def test_sc_gen_all_fills_missing_and_stale(logger_setup, tmp_path):
    ws = _FakeWorkshop()
    ui, store, rt, applied, sent = _make_ui(tmp_path, workshop=ws)
    pid = rt.profile_id
    _seed_items(store, pid)
    store.add_voice(pid, "v1", "标准")
    store.add_voice(pid, "v2", "情绪")

    # 2 条目 × 2 音色 = 4 条待生成；逐条进度 + 完成广播
    asyncio.run(_act_async(ui, {"action": "sc_gen_all", "profile_id": pid}))
    done_msg = _script_msgs(sent, "gen_done")[-1]
    assert done_msg["total"] == 4 and done_msg["done"] == 4
    assert done_msg["failed"] == [] and done_msg["cancelled"] is False
    assert len(_script_msgs(sent, "gen_progress")) == 4
    p = store.get_profile(pid)
    for section in ("topics_ai", "samples_ai"):
        for item in p[section]:
            assert set(item["audios"]) == {"v1", "v2"}
    assert len(applied) == 1

    # 全部就绪 → 第二轮 total=0，不再合成
    ws.synth_calls.clear()
    asyncio.run(_act_async(ui, {"action": "sc_gen_all", "profile_id": pid}))
    assert _script_msgs(sent, "gen_done")[-1]["total"] == 0
    assert ws.synth_calls == []

    # 文本改过后 → 该条目全部音色 stale，仅补齐 stale 组合
    p["topics_ai"][0]["text"] = "新话题文本"
    store.save_profile(pid, p)
    asyncio.run(_act_async(ui, {"action": "sc_gen_all", "profile_id": pid}))
    dm = _script_msgs(sent, "gen_done")[-1]
    assert dm["total"] == 2 and dm["done"] == 2   # t1×v1、t1×v2

    # 限定音色批量：只补 v2 的样本
    p = store.get_profile(pid)
    p["samples_ai"][0]["audios"].pop("v2")
    store.save_profile(pid, p, guard=False)
    asyncio.run(_act_async(ui, {"action": "sc_gen_all", "profile_id": pid,
                                "voice_id": "v2"}))
    dm2 = _script_msgs(sent, "gen_done")[-1]
    assert dm2["total"] == 1 and dm2["done"] == 1


def test_sc_gen_all_collects_failures(logger_setup, tmp_path):
    ui, store, rt, applied, sent = _make_ui(tmp_path)
    pid = rt.profile_id
    _seed_items(store, pid)
    store.add_voice(pid, "v1", "标准")
    store.add_voice(pid, "bad_voice", "坏的")

    asyncio.run(_act_async(ui, {"action": "sc_gen_all", "profile_id": pid}))
    dm = _script_msgs(sent, "gen_done")[-1]
    assert dm["total"] == 4 and dm["done"] == 2
    assert len(dm["failed"]) == 2
    assert {f["voice_id"] for f in dm["failed"]} == {"bad_voice"}
    # 成功音色的音频已入库，失败音色无引用
    p = store.get_profile(pid)
    assert p["topics_ai"][0]["audios"]["v1"]["default"]["file"]
    assert "bad_voice" not in p["topics_ai"][0]["audios"]


def test_sc_gen_all_cancel(logger_setup, tmp_path):
    ws = _FakeWorkshop()
    ui, store, rt, applied, sent = _make_ui(tmp_path, workshop=ws)
    pid = rt.profile_id
    _seed_items(store, pid)
    store.add_voice(pid, "v1", "标准")
    # 第一条合成开始时置取消标记 → 后续条目不再合成
    ws.on_synth = lambda: ui._sc_gen_cancel(pid)
    asyncio.run(_act_async(ui, {"action": "sc_gen_all", "profile_id": pid}))
    dm = _script_msgs(sent, "gen_done")[-1]
    assert dm["cancelled"] is True
    assert dm["done"] == 1 and dm["total"] == 2


# ---------- 情绪档位动作族（v4） ----------

def test_sc_emotion_list_save_gen_purge(logger_setup, tmp_path):
    ws = _FakeWorkshop()
    ui, store, rt, applied, sent = _make_ui(tmp_path, workshop=ws)
    pid = rt.profile_id
    _seed_items(store, pid)
    store.add_voice(pid, "v1", "标准")

    # 列出：8 枚举可选清单 + 已登记档位 + 逐档缺失统计
    _act(ui, {"action": "sc_emotion_list", "profile_id": pid})
    st = _script_msgs(sent, "emotions")[-1]
    assert st["available"][0] == "default" and "happy" in st["available"]
    assert st["emotions"] == ["default"]
    assert st["voice_id"] == "v1" and st["total"] == 2
    assert st["missing"] == {"default": 2}

    # 保存档位：勾选 happy + calm（default 恒在，非法档丢弃）
    asyncio.run(_act_async(ui, {"action": "sc_emotion_save", "profile_id": pid,
                                "emotions": ["happy", "calm", "吼叫"]}))
    sv = _script_msgs(sent, "emotions_saved")[-1]
    assert sv["emotions"] == ["default", "happy", "calm"]
    assert len(applied) == 1
    _act(ui, {"action": "sc_emotion_list", "profile_id": pid})
    assert _script_msgs(sent, "emotions")[-1]["missing"] == \
        {"default": 2, "happy": 2, "calm": 2}

    # 按档位批量生成：只补 happy 档（2 条目 × 1 音色），default/calm 不受影响
    ws.synth_calls.clear()
    asyncio.run(_act_async(ui, {"action": "sc_emotion_gen", "profile_id": pid,
                                "voice_id": "v1", "emotion": "happy"}))
    dm = _script_msgs(sent, "gen_done")[-1]
    assert dm["total"] == 2 and dm["done"] == 2 and dm["failed"] == []
    assert len(ws.synth_calls) == 2
    p = store.get_profile(pid)
    assert p["topics_ai"][0]["audios"]["v1"]["happy"]["file"]
    assert "default" not in p["topics_ai"][0]["audios"]["v1"]
    _act(ui, {"action": "sc_emotion_list", "profile_id": pid})
    assert _script_msgs(sent, "emotions")[-1]["missing"] == \
        {"default": 2, "happy": 0, "calm": 2}

    # 未登记档位直接补生成：自动登记，避免"生成了却用不上"
    asyncio.run(_act_async(ui, {"action": "sc_emotion_gen", "profile_id": pid,
                                "voice_id": "v1", "emotion": "surprised"}))
    assert _script_msgs(sent, "gen_done")[-1]["total"] == 2
    assert "surprised" in store.get_profile(pid)["emotions"]

    # 清空 happy 档：引用与文件一起删掉，档位移出集合
    happy_file = p["topics_ai"][0]["audios"]["v1"]["happy"]["file"]
    asyncio.run(_act_async(ui, {"action": "sc_emotion_purge", "profile_id": pid,
                                "voice_id": "v1", "emotion": "happy"}))
    pg = _script_msgs(sent, "emotion_purged")[-1]
    assert pg["files"] == 2 and pg["emotion"] == "happy"
    # 档位顺序按 8 枚举原序（surprised 在 calm 之前）
    assert pg["emotions"] == ["default", "surprised", "calm"]
    assert not store.audio_exists(pid, happy_file)

    # default 拒删；非法档位可读报错
    asyncio.run(_act_async(ui, {"action": "sc_emotion_purge", "profile_id": pid,
                                "voice_id": "v1", "emotion": "default"}))
    assert "不可清空" in _script_msgs(sent, "error")[-1]["error"]


def test_sc_gen_all_covers_emotion_dimension(logger_setup, tmp_path):
    """批量补齐按「音色 × 档位」展开；已就绪组合不重复合成。"""
    ws = _FakeWorkshop()
    ui, store, rt, applied, sent = _make_ui(tmp_path, workshop=ws)
    pid = rt.profile_id
    _seed_items(store, pid)
    store.add_voice(pid, "v1", "标准")
    store.set_emotions(pid, ["happy"])

    asyncio.run(_act_async(ui, {"action": "sc_gen_all", "profile_id": pid}))
    dm = _script_msgs(sent, "gen_done")[-1]
    assert dm["total"] == 4 and dm["done"] == 4        # 2 条目 × 1 音色 × 2 档
    slots = store.get_profile(pid)["topics_ai"][0]["audios"]["v1"]
    assert set(slots) == {"default", "happy"}
    # 第二遍无事可做
    ws.synth_calls.clear()
    asyncio.run(_act_async(ui, {"action": "sc_gen_all", "profile_id": pid}))
    assert _script_msgs(sent, "gen_done")[-1]["total"] == 0
    assert ws.synth_calls == []
    # 引用在但文件被删 → 仍算缺失，会被补回（只删了话题那条）
    store.delete_audio(pid, slots["happy"]["file"])
    asyncio.run(_act_async(ui, {"action": "sc_gen_all", "profile_id": pid}))
    assert _script_msgs(sent, "gen_done")[-1]["total"] == 1


# ---------- Cookie 卡 ----------

def test_cookie_get_and_set(logger_setup, tmp_path):
    ui, store, rt, applied, sent = _make_ui(
        tmp_path, cfg={"danmaku": {"cookie": "ttwid=abc; fpk=1"}})
    _act(ui, {"action": "cookie_get"})
    st = _script_msgs(sent, "cookie")[-1]
    assert st["configured"] is True and st["chars"] > 0 and "****" in st["preview"]

    calls = []

    async def fake_configurer(cookie):
        calls.append(cookie)
        ui._config["danmaku"]["cookie"] = cookie
        return (True, "Cookie 已保存")

    ui.cookie_configurer = fake_configurer
    asyncio.run(_act_async(ui, {"action": "cookie_set",
                                "cookie": " ttwid=abc; sessionid=s1  "}))
    assert calls == [" ttwid=abc; sessionid=s1  "]
    done = _script_msgs(sent, "cookie_saved")[-1]
    assert done["ok"] is True and done["logged_in"] is True
    notes = [e["text"] for e in ui._feed if e["kind"] == "system"]
    assert any("Cookie 已保存" in t for t in notes)

    # configurer 抛异常 → 可读广播，不炸连接
    async def boom(cookie):
        raise RuntimeError("磁盘只读")

    ui.cookie_configurer = boom
    asyncio.run(_act_async(ui, {"action": "cookie_set", "cookie": "x"}))
    fail = _script_msgs(sent, "cookie_saved")[-1]
    assert fail["ok"] is False and "磁盘只读" in fail["message"]


# ---------- HTTP：zip 导出 / 音频直链 ----------

def test_http_export_and_audio(logger_setup, tmp_path):
    import urllib.request
    from websockets.asyncio.client import connect  # noqa: F401  保持与 test_webui 同依赖面

    ui, store, rt, applied, sent = _make_ui(tmp_path)
    ui._enabled = True
    pid = rt.profile_id
    audio_name = store.save_audio(pid, _wav_bytes(), "topic.wav")

    async def scenario():
        await ui.start()
        try:
            def _fetch(url):
                with urllib.request.urlopen(url, timeout=5) as resp:
                    return resp.status, dict(resp.headers), resp.read()

            status, headers, raw = await asyncio.to_thread(
                _fetch, f"http://127.0.0.1:{ui.port}/sc_export?pid={pid}")
            assert status == 200
            assert "attachment" in headers.get("Content-Disposition", "")
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                assert "profile.yaml" in zf.namelist()
                assert f"audio/{audio_name}" in zf.namelist()

            status, headers, raw = await asyncio.to_thread(
                _fetch, f"http://127.0.0.1:{ui.port}/sc_audio?pid={pid}&name={audio_name}")
            assert status == 200
            assert headers.get("Content-Type") == "audio/wav"
            assert raw[:4] == b"RIFF"
        finally:
            await ui.close()

    asyncio.run(scenario())


def test_sc_export_action_saves_zip(tmp_path, monkeypatch):
    """WS sc_export 动作：缺省落「下载」文件夹并回传路径（浏览器模式）。"""
    from pathlib import Path as _P
    fake_home = tmp_path / "home"
    monkeypatch.setattr(_P, "home", classmethod(lambda cls: fake_home))

    ui, store, rt, applied, sent = _make_ui(tmp_path)
    pid = rt.profile_id

    async def scenario():
        await ui._handle_action(json.dumps({"action": "sc_export", "profile_id": pid}), {})
        await asyncio.sleep(0.5)

    asyncio.run(scenario())
    msg = _script_msgs(sent, "exported")[-1]
    assert msg["pid"] == pid
    dest = fake_home / "Downloads" / f"zaizaishu-profile-{pid}.zip"
    assert dest.is_file()
    with zipfile.ZipFile(dest) as zf:
        assert "profile.yaml" in zf.namelist()


def test_sc_export_action_with_custom_path(logger_setup, tmp_path):
    """WS sc_export 带 path（桌面端「另存为」对话框给的路径）→ 存到指定位置。"""
    ui, store, rt, applied, sent = _make_ui(tmp_path)
    pid = rt.profile_id
    dest = tmp_path / "自选位置" / "我的存档"      # 无 .zip 后缀 → 自动补

    async def scenario():
        await ui._handle_action(json.dumps(
            {"action": "sc_export", "profile_id": pid, "path": str(dest)}), {})
        await asyncio.sleep(0.5)

    asyncio.run(scenario())
    msg = _script_msgs(sent, "exported")[-1]
    out = tmp_path / "自选位置" / "我的存档.zip"
    assert msg["path"] == str(out)
    assert out.is_file()
    with zipfile.ZipFile(out) as zf:
        assert "profile.yaml" in zf.namelist()


def test_sc_actions_without_store_are_readable(logger_setup, tmp_path):
    """未装配 script_store 时：sc_* 动作回报可读错误而非静默。"""
    ui = WebUi(config={"webui": {"enabled": False}}, stats=StatsCollector())
    sent = []
    ui._broadcast = lambda msg: sent.append(msg)
    _act(ui, {"action": "sc_list"})
    err = _script_msgs(sent, "error")[-1]
    assert "未就绪" in err["error"]


def test_toast_broadcasts_capsule_notification(logger_setup, tmp_path):
    """toast() 广播 {type:toast, data:{level,text}}，供前端渲染顶部胶囊通知。"""
    ui = WebUi(config={"webui": {"enabled": False}}, stats=StatsCollector())
    sent = []
    ui._broadcast = lambda msg: sent.append(msg)
    ui.toast("话术已保存", "success")
    ui.toast("合成失败：余额不足", "error")
    ui.toast("正在连接房间 123", "info")
    toasts = [m for m in sent if m.get("type") == "toast"]
    assert [t["data"] for t in toasts] == [
        {"level": "success", "text": "话术已保存"},
        {"level": "error", "text": "合成失败：余额不足"},
        {"level": "info", "text": "正在连接房间 123"},
    ]
