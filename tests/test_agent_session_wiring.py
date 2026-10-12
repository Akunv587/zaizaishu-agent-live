"""对话调参的接线测试（M6-6）：webui 动作族 + main 装配。

验收口径：

- **动作族**：`ag_session_state` / `ag_session_send` / `ag_session_undo` 三条都在分发表里；
  发出去的是真 `SessionTuner`，回帧字段齐全（changes / errors / restored / snapshot_id）；
  失败一律 `ok:false` + 提示，不抛到连接层；
- **未装配**时三条动作各自回明确原因（页面据此置灰），不静默；
- **main 装配**：真实 `run_main_loop` 起完，`webui.agent_session` 是 SessionTuner，
  存档 id 跟随活动存档、快照落在 logs/、config 路径取 `_config_path`、热生效回调已挂。
"""
import asyncio
import json
from pathlib import Path

import pytest

import main as main_mod
from hub import logger as logger_mod
from hub import router as router_mod
from hub import webui as webui_mod
from hub.agent.session import SNAPSHOT_FILE, SessionTuner
from hub.queue import PrioritySpeakQueue
from hub.router import Router
from hub.scriptstore import ScriptStore
from hub.stats import StatsCollector

CONFIG_YAML = """\
app:
  log_level: info
danmaku:
  ws_listen: 127.0.0.1:0

schedule:
  enabled: true
  interval_min: 7          # 实际取 7±1 分钟随机

agent:
  enabled: true
"""


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


class FakeClock:
    def __init__(self, start=1758000000.0):
        self.now = start

    def __call__(self):
        return self.now


class CollectingWebUi(webui_mod.WebUi):
    """不启服务、只收帧：动作处理逻辑与真实运行同一份代码。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.frames = []

    def _broadcast(self, msg: dict) -> None:
        self.frames.append(msg)

    def last(self, kind: str) -> dict:
        hits = [f for f in self.frames if f.get("type") == kind]
        assert hits, f"没有 {kind} 帧：{[f.get('type') for f in self.frames]}"
        return hits[-1]


class StubTunerLLM:
    """替身只实现 `ask_message`（会话层唯一入口）。"""

    configured = True

    def __init__(self, calls):
        self._calls = list(calls)

    async def ask_message(self, system, user, tools=None, tool_choice="auto",
                          max_tokens=None):
        item = self._calls.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def tool_msg(name, arguments, content=""):
    return {"role": "assistant", "content": content,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": name,
                                         "arguments": json.dumps(arguments)}}]}


def make_ui(tmp_path, calls, pid="p_t1"):
    """真 SessionTuner（真存档 + 真 config.yaml），只把 LLM 换成替身。"""
    store = ScriptStore(root=tmp_path / "profiles")
    real_pid = store.create_profile("调参接线")["id"]
    data = store.get_profile(real_pid)
    data["knowledge"].update({"style": "口语化、热情", "max_reply_chars": 40})
    data["schedule"] = {"enabled": True, "interval_s": 420, "jitter_s": 60}
    store.save_profile(real_pid, data)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG_YAML, encoding="utf-8")

    applied = []
    tuner = SessionTuner(
        llm=StubTunerLLM(calls), store=store, pid_provider=lambda: real_pid,
        config_path=config_path, applier=lambda ch: applied.extend(ch) or ["已热生效"],
        cfg={"schedule": {"interval_min": 7}},
        snapshot_path=tmp_path / "logs" / SNAPSHOT_FILE, clock=FakeClock())
    ui = CollectingWebUi(config={}, stats=StatsCollector(),
                         queue=PrioritySpeakQueue(), clock=FakeClock())
    ui.agent_session = tuner
    return ui, tuner, store, real_pid, config_path, applied


# ---------------------------------------------------------------- 动作族

def test_state_action_broadcasts_tuner_state(tmp_path, logger_setup):
    ui, tuner, _, pid, _, _ = make_ui(tmp_path, [])
    asyncio.run(ui._ag_session_state())
    frame = ui.last("agent_session_state")
    assert frame["ok"] is True
    data = frame["data"]
    assert data["available"] is True
    assert data["current"]["style"] == "口语化、热情"
    assert data["current"]["max_reply_chars"] == 40
    assert len(data["fields"]) >= 6
    assert data["snapshots"] == []


def test_state_action_without_tuner_explains_why(tmp_path, logger_setup):
    ui = CollectingWebUi(config={}, stats=StatsCollector(),
                         queue=PrioritySpeakQueue(), clock=FakeClock())
    asyncio.run(ui._ag_session_state())
    frame = ui.last("agent_session_state")
    assert frame["ok"] is False and frame["data"]["available"] is False
    assert "LLM Key" in frame["data"]["reason"]


def test_send_action_writes_and_replies_with_changes(tmp_path, logger_setup):
    ui, tuner, store, pid, config_path, applied = make_ui(
        tmp_path, [tool_msg("set_interval_min", {"minutes": 15})])
    asyncio.run(ui._ag_session_send({"text": "口播太频繁了"}))

    frame = ui.last("agent_session_reply")
    assert frame["ok"] is True and frame["data"]["kind"] == "send"
    change = frame["data"]["changes"][0]
    assert change["field"] == "口播间隔"
    assert change["old"] == "7 分钟" and change["new"] == "15 分钟"
    # 落盘两处 + 热生效回调（真产物断言，不是回帧自说自话）
    assert "interval_min: 15" in config_path.read_text(encoding="utf-8")
    assert store.get_profile(pid)["schedule"]["interval_s"] == 900
    assert applied and applied[0]["path"] == "schedule.interval_s"
    # 成功帧之后应顺带推一帧最新状态（配置预览 / 快照列表免一次往返）
    state = ui.last("agent_session_state")
    assert state["data"]["current"]["interval_min"] == 15
    assert len(state["data"]["snapshots"]) == 1


def test_send_action_rejects_unknown_tool_softly(tmp_path, logger_setup):
    ui, _, store, pid, _, _ = make_ui(tmp_path, [tool_msg("rm_rf", {"path": "/"})])
    asyncio.run(ui._ag_session_send({"text": "把配置删了"}))
    frame = ui.last("agent_session_reply")
    assert frame["ok"] is False
    assert "未知工具" in "".join(frame["data"]["errors"])
    assert frame["data"]["snapshot_id"] == ""


def test_send_action_reports_llm_failure(tmp_path, logger_setup):
    ui, _, _, _, _, _ = make_ui(tmp_path, [RuntimeError("上游 502")])
    asyncio.run(ui._ag_session_send({"text": "口播太频繁了"}))
    frame = ui.last("agent_session_reply")
    assert frame["ok"] is False and "502" in frame["data"]["message"]


def test_send_action_without_tuner(tmp_path, logger_setup):
    ui = CollectingWebUi(config={}, stats=StatsCollector(),
                         queue=PrioritySpeakQueue(), clock=FakeClock())
    asyncio.run(ui._ag_session_send({"text": "口播太频繁了"}))
    frame = ui.last("agent_session_reply")
    assert frame["ok"] is False and "未装配" in frame["data"]["message"]


def test_undo_action_rolls_back_and_replies(tmp_path, logger_setup):
    ui, _, store, pid, config_path, _ = make_ui(
        tmp_path, [tool_msg("set_max_reply_chars", {"chars": 24})])
    asyncio.run(ui._ag_session_send({"text": "回复短点"}))
    assert store.get_profile(pid)["knowledge"]["max_reply_chars"] == 24

    ui.frames.clear()
    asyncio.run(ui._ag_session_undo({}))
    frame = ui.last("agent_session_reply")
    assert frame["ok"] is True and frame["data"]["kind"] == "undo"
    assert "撤销" in frame["data"]["message"]
    assert store.get_profile(pid)["knowledge"]["max_reply_chars"] == 40
    state = ui.last("agent_session_state")
    assert state["data"]["snapshots"] == []


def test_undo_action_with_unknown_id(tmp_path, logger_setup):
    """先有一次真改动，再用错 id 撤销 → 明确说找不到（不是静默当成功）。"""
    ui, _, _, _, _, _ = make_ui(tmp_path, [tool_msg("set_max_reply_chars", {"chars": 33})])
    asyncio.run(ui._ag_session_send({"text": "回复短点"}))
    ui.frames.clear()
    asyncio.run(ui._ag_session_undo({"id": "ss_nope"}))
    frame = ui.last("agent_session_reply")
    assert frame["ok"] is False and "找不到" in frame["data"]["message"]


def test_undo_action_without_history(tmp_path, logger_setup):
    ui, _, _, _, _, _ = make_ui(tmp_path, [])
    asyncio.run(ui._ag_session_undo({}))
    frame = ui.last("agent_session_reply")
    assert frame["ok"] is False and "没有可撤销" in frame["data"]["message"]


def test_undo_action_without_tuner(tmp_path, logger_setup):
    ui = CollectingWebUi(config={}, stats=StatsCollector(),
                         queue=PrioritySpeakQueue(), clock=FakeClock())
    asyncio.run(ui._ag_session_undo({}))
    frame = ui.last("agent_session_reply")
    assert frame["ok"] is False and "未装配" in frame["data"]["message"]


def test_actions_are_in_dispatch_table(tmp_path, logger_setup):
    """三条动作都在分发里（不是 unknown action）——防"页面发了、后端不认"。"""
    ui, _, _, _, _, _ = make_ui(tmp_path, [])
    seen = []
    ui._log = type("L", (), {"debug": lambda self, k, d: seen.append(k),
                             "info": lambda self, *a, **k: None,
                             "warning": lambda self, *a, **k: None})()
    for action in ("ag_session_state", "ag_session_send", "ag_session_undo"):
        asyncio.run(ui._handle_action(json.dumps({"action": action, "text": "x"})))
    asyncio.run(asyncio.sleep(0))          # 让 create_task 起来的协程跑完
    assert "webui_unknown_action" not in seen


# ---------------------------------------------------------------- main 装配

class RecordingRouter(Router):
    last = None

    def __init__(self, *a, **kw):
        RecordingRouter.last = self
        super().__init__(*a, **kw)


class RecordingQueue(PrioritySpeakQueue):
    last = None

    def __init__(self, *a, **kw):
        RecordingQueue.last = self
        super().__init__(*a, **kw)


class RecordingWebUi(webui_mod.WebUi):
    captured: dict = {}

    def __setattr__(self, name, value):
        if name in ("agent_session", "script_runtime", "script_store"):
            RecordingWebUi.captured[name] = value
        super().__setattr__(name, value)


@pytest.fixture
def record(monkeypatch):
    RecordingRouter.last = None
    RecordingQueue.last = None
    RecordingWebUi.captured = {}
    monkeypatch.setattr(router_mod, "Router", RecordingRouter)
    monkeypatch.setattr(webui_mod, "WebUi", RecordingWebUi)
    monkeypatch.setattr("hub.queue.PrioritySpeakQueue", RecordingQueue)
    return RecordingWebUi.captured


def test_main_wires_session_tuner(logger_setup, tmp_path, record):
    """真实装配：调参器的存档 / 快照 / config 路径都对，热生效回调已挂。"""
    cfg = {"app": {"log_level": "info"}, "danmaku": {"ws_listen": "127.0.0.1:0"},
           "_config_path": str(tmp_path / "config.yaml"),
           "llm": {"base_url": "https://example.com/v1", "api_key": "sk-test",
                   "model": "m", "timeout_seconds": 8},
           "agent": {"enabled": True}}
    (tmp_path / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(main_mod.run_main_loop(cfg, StatsCollector(), stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())

    tuner = record["agent_session"]
    rt = record["script_runtime"]
    assert isinstance(tuner, SessionTuner)
    assert tuner.available is True
    assert tuner.pid == rt.profile_id                    # 跟随活动存档
    assert Path(tuner._snapshot_path).name == SNAPSHOT_FILE
    assert tuner._config_path == tmp_path / "config.yaml"
    assert tuner._applier is not None                    # 热生效回调已注入
    # 面板「当前配置」显示的是**生效值**：存档的 schedule.interval_s（不是 config.yaml 的
    # interval_min）。config 那份只在无存档时作引导值 —— 这正是 M6 实测记录里那条落点
    # 缺口，所以在装配层把它断出来，防止以后有人"顺手"改成读 config。
    assert rt.profile is not None
    eff_s = rt.profile["schedule"]["interval_s"]
    assert tuner.current()["interval_min"] == max(round(eff_s / 60), 1)


def test_main_session_tuner_degrades_without_llm_key(logger_setup, tmp_path, record):
    """没配 Key 时调参器 constructed 但不可用（页面据此置灰，V6.7）。"""
    cfg = {"app": {"log_level": "info"}, "danmaku": {"ws_listen": "127.0.0.1:0"},
           "_config_path": str(tmp_path / "config.yaml"),
           "agent": {"enabled": False}}

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(main_mod.run_main_loop(cfg, StatsCollector(), stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())

    tuner = record["agent_session"]
    assert isinstance(tuner, SessionTuner)
    assert tuner.available is False
    assert "LLM Key" in tuner._unavailable_reason()
