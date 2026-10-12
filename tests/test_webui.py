"""hub.webui 可视化控制台的单元测试（用户需求：网页查看/操控中枢）。

覆盖：HTTP 页面服务、快照字段完整性、事件流推送与回放、控制动作
（暂停 toggle 转发、测试事件注入）、enabled=false 时不监听。
"""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from websockets.asyncio.client import connect

from hub.stats import StatsCollector
from hub.webui import WebUi


@pytest.fixture
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "debug")
    return tmp_path


class _FakeHotkey:
    def __init__(self):
        self.paused = False
        self.toggled = 0

    async def toggle(self):
        self.toggled += 1
        self.paused = not self.paused


class _FakeQueue:
    def qstat(self):
        return {"p0": 0, "p1": 2, "p2": 0, "total": 2}


def _make_event(ev_type="chat", user="观众A", content="主播这是什么游戏"):
    ev = SimpleNamespace(type=ev_type, user=user, content=content,
                         gift_name="小心意", gift_count=2, gift_value=100,
                         combo_end=True, ts=time.time())
    return ev


def _make_task(text="欢迎来到直播间", source="chat", priority=1):
    return SimpleNamespace(text=text, source=source, priority=priority,
                           tier=None, merged_count=3)


def _make_ui(**kw):
    cfg = {"webui": {"enabled": True, "listen": "127.0.0.1:0"},
           "llm": {"model": "gpt-test", "base_url": "https://x/v1"},
           "tts": {"voice_id": "myVoice"},
           "audio": {"device": "CABLE", "sample_rate": 24000},
           "watchdog": {"fail_threshold": 10}}
    stats = StatsCollector()
    stats.inc("danmaku_in", 5)
    stats.inc("played", 4)
    ui = WebUi(config=cfg, stats=stats, queue=_FakeQueue(), **kw)
    return ui


def test_disabled_by_default_when_no_config():
    ui = WebUi(config={}, stats=StatsCollector())
    assert ui.enabled is False
    assert ui._server is None


def test_snapshot_contains_full_state(logger_setup):
    hotkey = _FakeHotkey()
    ui = _make_ui(hotkey=hotkey)
    ui.note_event(_make_event())
    snap = ui._build_snapshot()
    assert snap["paused"] is False
    assert snap["queue"]["p1"] == 2
    assert snap["stats"]["danmaku_in"] == 5
    assert snap["stats"]["played"] == 4
    assert snap["last_event_age_s"] is not None
    assert snap["config"]["llm_model"] == "gpt-test"
    assert snap["config"]["voice_id"] == "myVoice"
    hotkey.paused = True
    assert ui._build_snapshot()["paused"] is True


def test_feed_ring_buffer_and_note_system(logger_setup):
    ui = _make_ui()
    ui.note_system("看门狗 · 测试告警")
    for i in range(120):
        ui.note_event(_make_event(content=f"弹幕{i}"))
    assert len(ui._feed) == 100          # 环形上限
    assert ui._feed[-1]["content"] == "弹幕119"
    assert ui._feed[0]["content"] == "弹幕20"   # system 条目已被挤出环形缓冲


def test_page_served_and_actions_roundtrip(logger_setup):
    """端到端：HTTP GET / 返回页面，WS /ws 收快照+feed，回发动作生效。"""
    hotkey = _FakeHotkey()
    tests = []
    ui = _make_ui(hotkey=hotkey)
    def _record_test(kind, payload=None):
        tests.append(kind)
    ui.test_event_sender = _record_test
    ui.source_state = lambda: {"mode": "live", "connected": 1, "listen": "127.0.0.1:8765"}
    ui.note_event(_make_event())

    async def scenario():
        await ui.start()
        try:
            # HTTP 页面（urlopen 是阻塞调用，必须让出事件循环：服务器也靠它跑）
            import urllib.request

            def _fetch():
                with urllib.request.urlopen(f"http://127.0.0.1:{ui.port}/", timeout=5) as resp:
                    return resp.read().decode("utf-8")

            body = await asyncio.to_thread(_fetch)
            assert "仔仔鼠" in body and "实时事件" in body and 'id="feed"' in body

            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                first = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert first["type"] == "snapshot"
                assert first["source"]["connected"] == 1
                history = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert history["type"] == "feed"
                assert history["data"][-1]["kind"] == "danmaku"

                # 控制动作：toggle_pause / pause（重复动作不抖动）
                await ws.send(json.dumps({"action": "toggle_pause"}))
                await asyncio.sleep(0.1)
                await ws.send(json.dumps({"action": "pause"}))   # 已暂停，不再 toggle
                await asyncio.sleep(0.1)
                await ws.send(json.dumps({"action": "test", "kind": "gift"}))
                await asyncio.sleep(0.1)
                assert hotkey.toggled == 1
                assert hotkey.paused is True
                assert tests == ["gift"]

                # 事件即时推送
                ui.note_system("系统测试通知")
                pushed = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                while pushed["type"] != "event":
                    pushed = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert pushed["data"]["kind"] == "system"
                assert pushed["data"]["text"] == "系统测试通知"
        finally:
            await ui.close()

    asyncio.run(scenario())
    assert ui._server is None


def test_set_room_action(logger_setup):
    """控制台贴房间号：set_room 动作调用 room_connector，空房间号只提示不调用。"""
    rooms = []
    ui = _make_ui()
    ui.room_connector = rooms.append

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()  # snapshot
                await ws.recv()  # feed

                await ws.send(json.dumps({"action": "set_room",
                                          "room": " 231946351964 "}))
                await ws.send(json.dumps({"action": "set_room",
                                          "room": "https://live.douyin.com/8888?x=1"}))
                await ws.send(json.dumps({"action": "set_room", "room": "  "}))
                await asyncio.sleep(0.2)

                # 空房间号 → note_system 提示进入事件流
                notes = [e["text"] for e in ui._feed if e["kind"] == "system"]
                assert any("请先粘贴房间号" in t for t in notes)
        finally:
            await ui.close()

    asyncio.run(scenario())
    assert rooms == ["231946351964", "https://live.douyin.com/8888?x=1"]


def test_page_contains_room_input(logger_setup):
    """控制台页包含房间号输入与连接按钮（贴房间号连接入口）。"""
    ui = _make_ui()
    page = ui._page
    assert 'id="roomInput"' in page
    assert 'id="btnConnect"' in page
    assert "set_room" in page


def test_page_contains_enter_fold(logger_setup):
    """事件流进场折叠：连续进场合并为一行「甲、乙 等 N 人进入直播间」，
    防热场期数百条进场淹没弹幕/礼物等有效事件。"""
    ui = _make_ui()
    page = ui._page
    assert 'dataset.fold === "enter"' in page
    assert "进入直播间" in page


def test_snapshot_contains_llm_status(logger_setup):
    """快照带 LLM 配置状态与脱敏密钥（前端配置表单的数据源）。"""
    ui = _make_ui()                      # _make_ui 的 llm 无 api_key → 未配置
    snap = ui._build_snapshot()
    assert snap["config"]["llm_configured"] is False
    assert snap["config"]["llm_key_masked"] == "<空>"
    ui._config["llm"]["api_key"] = "sk-real-key"
    snap = ui._build_snapshot()
    assert snap["config"]["llm_configured"] is True
    assert snap["config"]["llm_key_masked"] == "sk-r****"


def test_set_llm_action(logger_setup):
    """控制台保存 LLM 配置：set_llm 动作转交 llm_configurer；异常经系统通知反馈。"""
    calls = []

    async def fake_configurer(base_url, api_key, model, test):
        calls.append((base_url, api_key, model, test))
        if base_url == "boom":
            raise RuntimeError("炸了")

    ui = _make_ui()
    ui.llm_configurer = fake_configurer

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()  # snapshot
                await ws.recv()  # feed
                await ws.send(json.dumps({"action": "set_llm",
                                          "base_url": "https://api.x.com/v1",
                                          "api_key": "", "model": "gpt-4o-mini",
                                          "test": True}))
                await ws.send(json.dumps({"action": "set_llm",
                                          "base_url": "boom", "api_key": "sk-k",
                                          "model": "m", "test": False}))
                await asyncio.sleep(0.2)
        finally:
            await ui.close()

    asyncio.run(scenario())
    assert calls == [("https://api.x.com/v1", "", "gpt-4o-mini", True),
                     ("boom", "sk-k", "m", False)]
    notes = [e["text"] for e in ui._feed if e["kind"] == "system"]
    assert any("LLM 配置失败" in t for t in notes)


def test_page_contains_llm_form(logger_setup):
    """控制台页包含 LLM 配置表单（无需配置密钥也可打开的填写入口）。"""
    ui = _make_ui()
    page = ui._page
    for needle in ('id="llmVendor"', 'id="llmBaseUrl"', 'id="llmApiKey"',
                   'id="llmModel"', 'id="llmTestBtn"', "set_llm"):
        assert needle in page


# ---------- 音色工坊（vc_* 动作与推送协议） ----------

class _FakeWorkshop:
    """音色工坊替身：记录调用并返回预设结果，不发网络请求。"""

    def __init__(self):
        self.uploads = []          # [(raw, name)]
        self.clones = []
        self.previews = []
        self.fetches = 0           # fetch_account_voices 调用次数
        self.params_saved = []     # [(voice_id, params)]

    def minimax_status(self):
        return {"configured": True, "endpoint": "https://api.minimaxi.com",
                "masked_key": "sk-mi****key"}

    def voices(self):
        return [{"voice_id": "zsVoice0001", "voice_name": "主播小美",
                 "created_at": "2026-09-04 09:00:00", "last_called": None}]

    def upload_audio(self, raw, name):
        self.uploads.append((raw, name))
        return {"file_id": 436449258881393, "purpose": "voice_clone", "name": name,
                "size": len(raw), "uploaded_at": "2026-09-04 09:00:00"}

    def clone_voice(self, file_id, voice_id, model=None, voice_name=None):
        self.clones.append((file_id, voice_id, model, voice_name))
        return {"voice_id": voice_id, "voice_name": voice_name or voice_id,
                "demo_audio": "https://demo/x.mp3"}

    def preview(self, text, voice_id, model=None):
        self.previews.append((text, voice_id, model))
        return {"audio_url": "https://audio/x.mp3", "voice_id": voice_id,
                "chars": len(text)}

    def fetch_account_voices(self):
        self.fetches += 1
        return {"added": [{"voice_id": "zsVoiceNew", "voice_name": "zsVoiceNew",
                           "source": "account_fetch", "created_at": "2026-09-13 10:00:00",
                           "last_called": None}],
                "system_count": 88,
                "voices": self.voices() + [{"voice_id": "zsVoiceNew",
                                            "voice_name": "zsVoiceNew"}]}

    def set_voice_params(self, voice_id, params):
        from hub.voiceworkshop import validate_voice_params
        cleaned = validate_voice_params(params)
        self.params_saved.append((voice_id, cleaned))
        return {"voice_id": voice_id, "params": cleaned}

    def voice_params(self, voice_id):
        return None

    def delete_voice(self, voice_id, live_voice_id=None):
        from hub.voiceworkshop import WorkshopError
        if live_voice_id and voice_id == live_voice_id:
            raise WorkshopError(f"「{voice_id}」是当前直播音色，请先切换")
        self.deleted = getattr(self, "deleted", [])
        self.deleted.append(voice_id)
        return {"voice_id": voice_id, "voice_type": "voice_cloning",
                "voices": [v for v in self.voices() if v.get("voice_id") != voice_id]}


async def _recv_type(ws, want: str, timeout: float = 2.0):
    """收到指定 type 的消息（跳过周期快照等无关帧）。"""
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if msg["type"] == want:
            return msg


def test_snapshot_contains_minimax_status(logger_setup):
    """快照带 MiniMax 配置状态（主控台「克隆音色」行与音色工坊入口的数据源）。"""
    ui = _make_ui()
    snap = ui._build_snapshot()
    assert snap["config"]["minimax_configured"] is False    # _make_ui 无 tts.api_key
    ui._config["tts"]["api_key"] = "sk-minimax-key"
    snap = ui._build_snapshot()
    assert snap["config"]["minimax_configured"] is True
    assert snap["config"]["minimax_key_masked"] == "sk-m****"


def test_page_contains_voice_workshop(logger_setup):
    """控制台页包含音色工坊标签页与全部功能入口（Key/上传/复刻/音色库/试听/拉取/参数微调）。"""
    ui = _make_ui()
    page = ui._page
    for needle in ('id="tabVoice"', 'id="btnGoVoice"', 'id="vcKey"', 'id="vcEndpoint"',
                   'id="vcKeySave"', 'id="vcFile"', 'id="vcCloneFile"', 'id="vcCloneId"',
                   'id="vcCloneBtn"', 'id="vcVoiceList"', 'id="vcPrevVoice"',
                   'id="vcPrevText"', 'id="vcPrevBtn"', 'id="vcAudio"',
                   'id="vcFetchBtn"', 'id="vcParamCard"', 'id="vcParamVoice"',
                   'id="vcPsSpeed"', 'id="vcPsVol"', 'id="vcPsPitch"',
                   'id="vcPsEmotion"', 'id="vcPsPrevBtn"', 'id="vcPsSaveBtn"',
                   'action: "vc_get"', "vc_set_key", "vc_set_voice",
                   "vc_upload", "vc_clone", "vc_preview",
                   "vw_fetch_voices", "vw_save_params", "vw_preview_params"):
        assert needle in page, needle


def test_vc_get_returns_status_and_voices(logger_setup):
    """vc_get → 广播 MiniMax 配置状态 + 音色库。"""
    workshop = _FakeWorkshop()
    ui = _make_ui(voice_workshop=workshop)

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()  # snapshot
                await ws.recv()  # feed
                await ws.send(json.dumps({"action": "vc_get"}))
                msg = await _recv_type(ws, "vc")
                assert msg["data"]["op"] == "status"
                assert msg["data"]["minimax"]["masked_key"] == "sk-mi****key"
                assert msg["data"]["voices"][0]["voice_id"] == "zsVoice0001"
        finally:
            await ui.close()

    asyncio.run(scenario())


def test_vc_set_key_and_voice_forward_to_configurer(logger_setup):
    """vc_set_key / vc_set_voice → tts_configurer（留空语义由 main._reconfigure_tts 处理）。"""
    calls = []

    async def fake_tts_configurer(api_key, endpoint, voice_id, test):
        calls.append((api_key, endpoint, voice_id, test))

    ui = _make_ui(voice_workshop=_FakeWorkshop())
    ui.tts_configurer = fake_tts_configurer

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()  # snapshot
                await ws.recv()  # feed
                await ws.send(json.dumps({"action": "vc_set_key", "api_key": "sk-new",
                                          "endpoint": "https://api.minimax.io",
                                          "test": True}))
                await ws.send(json.dumps({"action": "vc_set_voice",
                                          "voice_id": "zsVoice0001"}))
                await asyncio.sleep(0.2)
        finally:
            await ui.close()

    asyncio.run(scenario())
    assert calls == [("sk-new", "https://api.minimax.io", "", True),
                     ("", "", "zsVoice0001", False)]


def test_vc_upload_chunked_binary_end_to_end(logger_setup):
    """分片二进制上传：vc_upload → N 个二进制帧 → vc_upload_end → upload_done。"""
    workshop = _FakeWorkshop()
    ui = _make_ui(voice_workshop=workshop)

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()  # snapshot
                await ws.recv()  # feed
                await ws.send(json.dumps({"action": "vc_upload", "name": "我的声音.wav"}))
                part1, part2, part3 = b"RIFF\x00\x00\x10\x00WAVE"[:6], \
                    b"RIFF\x00\x00\x10\x00WAVE"[6:], b"0" * 300
                await ws.send(part1)
                await ws.send(part2)
                await ws.send(part3)
                await ws.send(json.dumps({"action": "vc_upload_end"}))
                msg = await _recv_type(ws, "vc")
                assert msg["data"]["op"] == "upload_done"
                assert msg["data"]["record"]["name"] == "我的声音.wav"
                assert workshop.uploads == [(part1 + part2 + part3, "我的声音.wav")]
        finally:
            await ui.close()

    asyncio.run(scenario())


def test_vc_upload_abort_and_no_session(logger_setup):
    """abort 清缓冲；未开始上传就 end → 可读错误广播。"""
    workshop = _FakeWorkshop()
    ui = _make_ui(voice_workshop=workshop)

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()  # snapshot
                await ws.recv()  # feed
                # end without start
                await ws.send(json.dumps({"action": "vc_upload_end"}))
                msg = await _recv_type(ws, "vc")
                assert msg["data"]["op"] == "error"
                assert "上传会话不存在" in msg["data"]["error"]
                # start → abort → end 也视为会话不存在
                await ws.send(json.dumps({"action": "vc_upload", "name": "a.wav"}))
                await ws.send(b"RIFFdata")
                await ws.send(json.dumps({"action": "vc_upload_abort"}))
                await ws.send(json.dumps({"action": "vc_upload_end"}))
                msg = await _recv_type(ws, "vc")
                assert msg["data"]["op"] == "error"
                assert workshop.uploads == []                     # 中止的不落库
        finally:
            await ui.close()

    asyncio.run(scenario())


def test_vc_upload_total_limit_aborts(logger_setup, monkeypatch):
    """超过总量上限（默认 20MB，测试缩小到 1KB）即中止并广播错误。"""
    import hub.webui as webui_mod

    monkeypatch.setattr(webui_mod, "VC_UPLOAD_TOTAL_MAX", 1024)
    workshop = _FakeWorkshop()
    ui = _make_ui(voice_workshop=workshop)

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()  # snapshot
                await ws.recv()  # feed
                await ws.send(json.dumps({"action": "vc_upload", "name": "big.wav"}))
                await ws.send(b"x" * 600)
                await ws.send(b"x" * 600)                       # 累计 1200 > 1024
                msg = await _recv_type(ws, "vc")
                assert msg["data"]["op"] == "error"
                assert "20MB" in msg["data"]["error"] or "上限" in msg["data"]["error"]
        finally:
            await ui.close()

    asyncio.run(scenario())
    assert workshop.uploads == []


def test_vc_clone_and_preview_actions(logger_setup):
    """vc_clone / vc_preview → 工坊替身被调用并广播结果。"""
    workshop = _FakeWorkshop()
    ui = _make_ui(voice_workshop=workshop)

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()  # snapshot
                await ws.recv()  # feed
                await ws.send(json.dumps({"action": "vc_clone", "file_id": 436449258881393,
                                          "voice_id": "zsVoice0002",
                                          "voice_name": "主播阿鼠"}))
                msg = await _recv_type(ws, "vc")
                assert msg["data"]["op"] == "clone_done"
                assert msg["data"]["record"]["voice_name"] == "主播阿鼠"

                await ws.send(json.dumps({"action": "vc_preview", "voice_id": "zsVoice0002",
                                          "text": "家人们晚上好"}))
                msg = await _recv_type(ws, "vc")
                assert msg["data"]["op"] == "preview_done"
                assert msg["data"]["audio_url"] == "https://audio/x.mp3"
                assert msg["data"]["chars"] == 6
        finally:
            await ui.close()

    asyncio.run(scenario())
    assert workshop.clones == [(436449258881393, "zsVoice0002", None, "主播阿鼠")]
    assert workshop.previews == [("家人们晚上好", "zsVoice0002", None)]


def test_vc_workshop_error_becomes_readable_broadcast(logger_setup):
    """工坊抛 WorkshopError → error 广播（可读文案直达控制台音色工坊页）。"""
    from hub.voiceworkshop import WorkshopError

    class _ErrWorkshop(_FakeWorkshop):
        def upload_audio(self, raw, name):
            raise WorkshopError("不是一个有效的音频文件")

    ui = _make_ui(voice_workshop=_ErrWorkshop())

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()  # snapshot
                await ws.recv()  # feed
                await ws.send(json.dumps({"action": "vc_upload", "name": "a.wav"}))
                await ws.send(b"RIFF\x00\x00\x10\x00WAVE" + b"0" * 2000)
                await ws.send(json.dumps({"action": "vc_upload_end"}))
                msg = await _recv_type(ws, "vc")
                assert msg["data"]["op"] == "error"
                assert msg["data"]["stage"] == "upload"
                assert "音频文件" in msg["data"]["error"]
        finally:
            await ui.close()

    asyncio.run(scenario())


# ---------- 音色工坊扩展（vw_* 动作：账号音色拉取 + 参数微调） ----------

def test_vw_fetch_voices(logger_setup):
    """vw_fetch_voices → 拉取账号音色并广播 fetch_voices_done（含新增与系统音色数）。"""
    workshop = _FakeWorkshop()
    ui = _make_ui(voice_workshop=workshop)

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()  # snapshot
                await ws.recv()  # feed
                await ws.send(json.dumps({"action": "vw_fetch_voices"}))
                msg = await _recv_type(ws, "vc")
                assert msg["data"]["op"] == "fetch_voices_done"
                assert msg["data"]["system_count"] == 88
                assert any(v.get("voice_id") == "zsVoiceNew"
                           for v in msg["data"]["added"])
        finally:
            await ui.close()

    asyncio.run(scenario())
    assert workshop.fetches == 1


def test_vw_save_params(logger_setup):
    """vw_save_params → 调工坊保存参数并广播 params_saved（含规范化后参数）。"""
    workshop = _FakeWorkshop()
    ui = _make_ui(voice_workshop=workshop)

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()
                await ws.recv()
                await ws.send(json.dumps({
                    "action": "vw_save_params", "voice_id": "zsVoice0001",
                    "params": {"speed": 1.234, "vol": 2.567,
                                "pitch": 1.6, "emotion": "happy"}}))
                msg = await _recv_type(ws, "vc")
                assert msg["data"]["op"] == "params_saved"
                assert msg["data"]["voice_id"] == "zsVoice0001"
                # 服务端在调工坊前已经过 validate_voice_params 规范化
                p = msg["data"]["params"]
                assert p["speed"] == 1.23 and p["vol"] == 2.6
                assert p["pitch"] == 2 and p["emotion"] == "happy"
        finally:
            await ui.close()

    asyncio.run(scenario())
    assert workshop.params_saved == [("zsVoice0001",
                                      {"speed": 1.23, "vol": 2.6,
                                       "pitch": 2, "emotion": "happy"})]


def test_vw_save_params_rejects_out_of_range(logger_setup):
    """vw_save_params 参数越界 → error 广播（工坊层 WorkshopError 直达前端）。"""
    workshop = _FakeWorkshop()

    class _RejectWorkshop(_FakeWorkshop):
        def set_voice_params(self, voice_id, params):
            from hub.voiceworkshop import WorkshopError
            raise WorkshopError("语速需在 0.5~2.0 之间（当前 99.0）")

    ui = _make_ui(voice_workshop=_RejectWorkshop())

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()
                await ws.recv()
                await ws.send(json.dumps({
                    "action": "vw_save_params", "voice_id": "zsVoice0001",
                    "params": {"speed": 99}}))
                msg = await _recv_type(ws, "vc")
                assert msg["data"]["op"] == "error"
                assert msg["data"]["stage"] == "save_params"
                assert "0.5~2.0" in msg["data"]["error"]
        finally:
            await ui.close()

    asyncio.run(scenario())


def test_vw_preview_params_uses_previewer(logger_setup):
    """vw_preview_params → 走 main 装配的 voice_param_previewer（输出声卡链路）。"""
    workshop = _FakeWorkshop()
    ui = _make_ui(voice_workshop=workshop)
    preview_calls = []

    async def fake_previewer(voice_id, text, params):
        preview_calls.append((voice_id, text, dict(params)))
        return (True, "ok")

    ui.voice_param_previewer = fake_previewer

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()
                await ws.recv()
                await ws.send(json.dumps({
                    "action": "vw_preview_params",
                    "voice_id": "zsVoice0001",
                    "text": "家人们晚上好|欢迎来到直播间",
                    "params": {"speed": 1.5, "vol": 2.0,
                                "pitch": 1, "emotion": "happy"}}))
                msg = await _recv_type(ws, "vc")
                assert msg["data"]["op"] == "preview_params_done"
                assert msg["data"]["ok"] is True
                assert msg["data"]["voice_id"] == "zsVoice0001"
        finally:
            await ui.close()

    asyncio.run(scenario())
    # 预览器收到的是规范化后的参数
    assert preview_calls == [("zsVoice0001", "家人们晚上好|欢迎来到直播间",
                              {"speed": 1.5, "vol": 2.0,
                               "pitch": 1, "emotion": "happy"})]


def test_vw_preview_params_validates_first(logger_setup):
    """vw_preview_params 参数越界 → 直接 error，不调用预览器。"""
    workshop = _FakeWorkshop()
    ui = _make_ui(voice_workshop=workshop)
    called = []

    async def fake_previewer(voice_id, text, params):
        called.append(1)
        return (True, "ok")

    ui.voice_param_previewer = fake_previewer

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()
                await ws.recv()
                await ws.send(json.dumps({
                    "action": "vw_preview_params", "voice_id": "v",
                    "text": "hi", "params": {"speed": 99}}))
                msg = await _recv_type(ws, "vc")
                assert msg["data"]["op"] == "error"
                assert msg["data"]["stage"] == "preview_params"
        finally:
            await ui.close()

    asyncio.run(scenario())
    assert not called   # 校验失败时预览器未被调用


def test_vw_delete_voice(logger_setup):
    """vw_delete_voice → 删除非直播音色：广播 voice_deleted 且工坊收到调用。"""
    workshop = _FakeWorkshop()
    ui = _make_ui(voice_workshop=workshop)   # 配置里直播音色是 myVoice

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()
                await ws.recv()
                await ws.send(json.dumps({"action": "vw_delete_voice",
                                          "voice_id": "zsVoice0001"}))
                msg = await _recv_type(ws, "vc")
                assert msg["data"]["op"] == "voice_deleted"
                assert msg["data"]["voice_id"] == "zsVoice0001"
                assert all(v.get("voice_id") != "zsVoice0001"
                           for v in msg["data"]["voices"])
        finally:
            await ui.close()

    asyncio.run(scenario())
    assert workshop.deleted == ["zsVoice0001"]


def test_vw_delete_voice_blocks_live(logger_setup):
    """vw_delete_voice 删直播音色 → error 广播（工坊层安全闸），不调平台。"""
    workshop = _FakeWorkshop()
    ui = _make_ui(voice_workshop=workshop)   # 直播音色 myVoice

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                await ws.recv()
                await ws.recv()
                await ws.send(json.dumps({"action": "vw_delete_voice",
                                          "voice_id": "myVoice"}))
                msg = await _recv_type(ws, "vc")
                assert msg["data"]["op"] == "error"
                assert msg["data"]["stage"] == "delete_voice"
                assert "直播音色" in msg["data"]["error"]
        finally:
            await ui.close()

    asyncio.run(scenario())
    assert not getattr(workshop, "deleted", [])
