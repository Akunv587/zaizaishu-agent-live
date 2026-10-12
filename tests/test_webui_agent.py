"""hub.webui 智能体总开关（M3）的单元测试。

验收口径（V3.2/V3.3）：快照带 agent 段；ag_status 回推状态；
ag_set_enabled 在未配 LLM Key 时拒绝开启并给出明确原因；装配 switcher 后动作
透传并回推结果；未装配 switcher 时明确失败而不静默改配置。
"""
import asyncio
import json

import pytest
from websockets.asyncio.client import connect

from hub.stats import StatsCollector
from hub.webui import WebUi


@pytest.fixture
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "debug")
    return tmp_path


def make_ui(llm_configured=True, **kw):
    cfg = {"webui": {"enabled": True, "listen": "127.0.0.1:0"},
           "llm": {"model": "m", "base_url": "https://x/v1",
                   "api_key": "sk-real" if llm_configured else ""},
           "agent": {"enabled": False}}
    return WebUi(config=cfg, stats=StatsCollector(), queue=_FakeQueue(), **kw)


class _FakeQueue:
    def qstat(self):
        return {"p0": 0, "p1": 0, "p2": 0, "total": 0}


# ---------- 状态快照 ----------

def test_snapshot_contains_agent_block(logger_setup):
    """快照帧带 agent 段（前端卡片随快照刷新，无需额外请求）。"""
    ui = make_ui()
    snap = ui._build_snapshot()
    assert "agent" in snap
    assert snap["agent"]["enabled"] is False
    assert snap["agent"]["available"] is True
    assert snap["agent"]["mode"] == "rules"
    assert snap["agent"]["no_expire"] is False
    assert set(snap["agent"]["settings"]) == {
        "enabled", "retrieve", "memory", "feedback", "emotion", "decision_log"}


def test_agent_status_uses_provider_when_injected(logger_setup):
    """注入 provider 时用它的运行时口径（含 router/queue 真实模式）。"""
    ui = make_ui()
    ui.agent_status_provider = lambda: {
        "enabled": True, "available": True, "llm_configured": True,
        "mode": "agent", "no_expire": True, "settings": {"custom": 1},
    }
    st = ui._agent_status()
    assert st["enabled"] is True and st["mode"] == "agent" and st["no_expire"] is True
    assert st["settings"] == {"custom": 1}


def test_agent_status_falls_back_when_provider_fails(logger_setup):
    """provider 抛异常不拖垮快照：记警告并退回纯配置口径。"""
    ui = make_ui()

    def boom():
        raise RuntimeError("炸了")

    ui.agent_status_provider = boom
    st = ui._agent_status()
    assert st["enabled"] is False and st["mode"] == "rules"
    assert st["available"] is True


def test_agent_status_fills_missing_keys(logger_setup):
    """provider 返回残缺 dict 时补齐关键字段，前端不渲染空值。"""
    ui = make_ui()
    ui.agent_status_provider = lambda: {"enabled": True}
    st = ui._agent_status()
    assert st["mode"] == "agent"                     # enabled 推导
    assert st["no_expire"] is True
    assert "settings" in st and "available" in st


# ---------- 开关动作 ----------

def test_ag_set_enabled_without_switcher_reports_failure(logger_setup):
    """未装配 switcher 时明确失败，且不改本地 config（配置与运行时不能只改一份）。"""
    ui = make_ui()
    asyncio.run(ui._ag_set_enabled(True))
    assert ui._config["agent"]["enabled"] is False


def test_ag_set_enabled_refused_without_llm_key(logger_setup):
    """V3.3：未配 LLM Key 时拒绝开启，switcher 不被调用，回明确原因。"""
    ui = make_ui(llm_configured=False)
    calls = []

    async def switcher(enabled):
        calls.append(enabled)
        return True, "不该被调用"

    ui.agent_switcher = switcher
    asyncio.run(ui._ag_set_enabled(True))
    assert calls == []                               # 服务端兜底拦下
    st = ui._agent_status()
    assert st["enabled"] is False
    assert st["available"] is False


def test_ag_set_enabled_forwards_and_broadcasts(logger_setup):
    """装配 switcher 后动作透传；结果经 agent 广播回推（ok 供按钮复位）。"""
    ui = make_ui()
    calls = []
    sent = []
    ui._broadcast = lambda msg: sent.append(msg)

    async def switcher(enabled):
        calls.append(enabled)
        return True, f"已{'开启' if enabled else '关闭'}"

    ui.agent_switcher = switcher
    asyncio.run(ui._ag_set_enabled(True))
    assert calls == [True]
    agent_msgs = [m for m in sent if m.get("type") == "agent"]
    assert agent_msgs and agent_msgs[-1]["ok"] is True

    asyncio.run(ui._ag_set_enabled(False))
    assert calls == [True, False]
    agent_msgs = [m for m in sent if m.get("type") == "agent"]
    assert agent_msgs[-1]["ok"] is True


def test_ag_set_enabled_switcher_exception_is_contained(logger_setup):
    """switcher 抛异常时给失败反馈而不是把异常抛出到 WS 循环。"""
    ui = make_ui()
    sent = []
    ui._broadcast = lambda msg: sent.append(msg)

    async def boom(enabled):
        raise RuntimeError("切换炸了")

    ui.agent_switcher = boom
    asyncio.run(ui._ag_set_enabled(True))
    agent_msgs = [m for m in sent if m.get("type") == "agent"]
    assert agent_msgs and agent_msgs[-1]["ok"] is False


def test_ag_set_enabled_switcher_reports_failure(logger_setup):
    """switcher 返回 (False, 原因) 时照原样回推原因。"""
    ui = make_ui()
    sent = []
    ui._broadcast = lambda msg: sent.append(msg)

    async def switcher(enabled):
        return False, "写配置失败"

    ui.agent_switcher = switcher
    asyncio.run(ui._ag_set_enabled(True))
    assert [m for m in sent if m.get("type") == "agent"][-1]["ok"] is False


# ---------- 端到端（WS 动作） ----------

def test_ag_reply_carries_op_and_message(logger_setup):
    """回推 data 里带 op/ok/message：卡片就地留说明，快照帧不带 op 因此不冲掉。"""
    ui = make_ui()
    sent = []
    ui._broadcast = lambda msg: sent.append(msg)

    async def switcher(enabled):
        return True, "智能体已开启并接管对话"

    ui.agent_switcher = switcher
    asyncio.run(ui._ag_set_enabled(True))
    msg = [m for m in sent if m.get("type") == "agent"][-1]
    assert msg["ok"] is True
    assert msg["data"]["op"] == "ag_saved"
    assert msg["data"]["ok"] is True
    assert msg["data"]["message"] == "智能体已开启并接管对话"


def test_page_contains_agent_card(logger_setup):
    """页面含智能体卡的全部锚点：开关、状态徽章、模式标签、提示与档位摘要容器。"""
    ui = make_ui()
    body = ui._page
    for anchor in ('id="agentEnabled"', 'id="agentSwitchRow"', 'id="agentLab"',
                   'id="agentState"', 'id="agentModeTag"', 'id="agentSub"',
                   'id="agentNote"', 'id="agentTags"', 'id="agentTagsTitle"',
                   '<h3>智能体'):
        assert anchor in body, anchor
    # 开关与档位摘要的样式类齐备（零 emoji、颜色走 CSS 变量）
    for cls in (".ag-switch", ".ag-switch.on", ".ag-sub", ".ag-tag", ".ag-tag.dis",
                ".ag-body", ".ag-col", ".cfg-card.wide"):
        assert cls in body, cls
    assert "ag_set_enabled" in body and "ag_status" in body


def test_ag_actions_over_websocket(logger_setup):
    """端到端：ag_status 回推状态；ag_set_enabled 透传 switcher 并回推结果。"""
    ui = make_ui()
    calls = []

    async def switcher(enabled):
        calls.append(enabled)
        return True, "已开启并接管对话"

    ui.agent_switcher = switcher

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                first = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert first["type"] == "snapshot"
                assert first["agent"]["enabled"] is False     # 出厂 OFF（V3.1）

                await ws.send(json.dumps({"action": "ag_status"}))
                got = await _recv_type(ws, "agent")
                assert got["data"]["mode"] == "rules"

                await ws.send(json.dumps({"action": "ag_set_enabled", "enabled": True}))
                got = await _recv_type(ws, "agent")
                assert got["ok"] is True
                assert calls == [True]
        finally:
            await ui.close()

    asyncio.run(scenario())


async def _recv_type(ws, want: str) -> dict:
    """读到指定 type 的消息（快照每秒一帧会插队）。"""
    for _ in range(20):
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
        if msg.get("type") == want:
            return msg
    raise AssertionError(f"未收到 {want} 消息")
