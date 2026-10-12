"""hub.webui 智能体页面动作（M5）的单元测试：决策透视数据 + 试玩调试。

验收口径：
- `ag_logs` 把 provider 的数据原样广播（含 available / records / summary / path / note），
  limit 被夹在 1~AG_LOGS_MAX；未装配 provider 或 provider 抛异常时都要**给出说明文案**
  （页面不能只剩一片空白，用户会以为坏了）；
- `ag_playground` 支持两种载荷（`events` 逐条 / `batch` 一键造批次），
  失败回 `ok:false` + 人话原因，不把异常抛进 WS 循环；
- 端到端（真 WS）：两个动作都能往返。
"""
import asyncio
import json

import pytest
from websockets.asyncio.client import connect

from hub.stats import StatsCollector
from hub.webui import AG_LOGS_DEFAULT, AG_LOGS_MAX, WebUi


@pytest.fixture
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "debug")
    return tmp_path


class _FakeQueue:
    def qstat(self):
        return {"p0": 0, "p1": 0, "p2": 0, "total": 0}


def make_ui(**kw):
    cfg = {"webui": {"enabled": True, "listen": "127.0.0.1:0"},
           "llm": {"model": "m", "base_url": "https://x/v1", "api_key": "sk-real"},
           "agent": {"enabled": False}}
    return WebUi(config=cfg, stats=StatsCollector(), queue=_FakeQueue(), **kw)


def rec(**over):
    base = {"ts": "2026-09-22T23:41:07.000", "type": "chat", "user": "阿强",
            "event_line": "阿强：这游戏怎么玩", "should_reply": True, "priority": 1,
            "reason": "有效提问", "reply_text": "点小手柄下载", "tone": "热情",
            "emotion": "happy", "degrade": "", "latency_ms": 1200,
            "recall": ["sample:a3f1"], "prerecorded": False, "test": False}
    base.update(over)
    return base


def sent_of(ui):
    out = []
    ui._broadcast = lambda msg: out.append(msg)
    return out


# ---------------------------------------------------------------- ag_logs


def test_ag_logs_broadcasts_provider_payload(logger_setup):
    ui = make_ui()
    out = sent_of(ui)
    seen = []
    ui.agent_logs_provider = lambda limit: (seen.append(limit), {
        "available": True, "records": [rec()], "summary": {"total": 1},
        "path": "logs/agent-20260922.jsonl", "note": ""})[1]

    ui._ag_push_logs(None)

    msg = out[-1]
    assert msg["type"] == "agent_logs" and msg["ok"] is True
    data = msg["data"]
    assert data["records"][0]["reason"] == "有效提问"      # V5.1 判断依据带回来了
    assert data["summary"]["total"] == 1
    assert data["path"].endswith(".jsonl")
    assert seen == [AG_LOGS_DEFAULT]


@pytest.mark.parametrize("given, want", [
    (0, 1), (1, 1), (500, 500), (99999, AG_LOGS_MAX), (-7, 1),
    ("abc", AG_LOGS_DEFAULT), (None, AG_LOGS_DEFAULT),
])
def test_ag_logs_clamps_limit(logger_setup, given, want):
    """limit 一律夹到 1~AG_LOGS_MAX，非法值回落默认——别让手改请求把整天日志塞进一帧。"""
    ui = make_ui()
    sent_of(ui)
    seen = []
    ui.agent_logs_provider = lambda limit: (seen.append(limit), {"records": []})[1]
    ui._ag_push_logs(given)
    assert seen == [want]


def test_ag_logs_without_provider_explains_why(logger_setup):
    ui = make_ui()
    out = sent_of(ui)
    ui._ag_push_logs(None)
    msg = out[-1]
    assert msg["ok"] is False
    assert msg["data"]["available"] is False
    assert "开启总开关" in msg["data"]["note"]           # 页面有话说，不是空白


def test_ag_logs_provider_failure_is_contained(logger_setup):
    ui = make_ui()
    out = sent_of(ui)

    def boom(limit):
        raise OSError("磁盘掉了")

    ui.agent_logs_provider = boom
    ui._ag_push_logs(None)
    msg = out[-1]
    assert msg["data"]["available"] is True and msg["data"]["records"] == []
    assert "读取决策日志失败" in msg["data"]["note"]


def test_ag_logs_fills_missing_keys(logger_setup):
    """provider 返回残缺 dict 时补齐，前端不渲染空值。"""
    ui = make_ui()
    out = sent_of(ui)
    ui.agent_logs_provider = lambda limit: {}
    ui._ag_push_logs(None)
    data = out[-1]["data"]
    assert data == {"available": True, "records": [], "summary": {}, "path": "", "note": ""}


# ---------------------------------------------------------------- ag_playground


def test_playground_payload_accepts_events_and_batch(logger_setup):
    assert WebUi._playground_payload({"events": [{"type": "chat"}]}) == {
        "events": [{"type": "chat"}]}
    assert WebUi._playground_payload({"batch": {"type": "chat", "count": 3}}) == {
        "batch": {"type": "chat", "count": 3}}
    assert WebUi._playground_payload({}) is None
    assert WebUi._playground_payload({"events": []}) is None
    assert WebUi._playground_payload({"batch": {}}) is None


def test_ag_playground_without_runner_explains_why(logger_setup):
    ui = make_ui()
    out = sent_of(ui)
    asyncio.run(ui._ag_playground({"events": [{"type": "chat"}]}))
    msg = out[-1]
    assert msg["type"] == "agent_playground" and msg["ok"] is False
    assert "未装配" in msg["data"]["message"]


def test_ag_playground_empty_request_is_rejected(logger_setup):
    ui = make_ui()
    out = sent_of(ui)
    ui.agent_playground = _never_called()
    asyncio.run(ui._ag_playground({}))
    assert out[-1]["ok"] is False
    assert "为空" in out[-1]["data"]["message"]


def _never_called():
    async def runner(payload):
        raise AssertionError("空请求不该调到执行器")
    return runner


def test_ag_playground_broadcasts_results(logger_setup):
    ui = make_ui()
    out = sent_of(ui)
    calls = []

    async def runner(payload):
        calls.append(payload)
        return {"results": [{"index": 0, "should_reply": True,
                             "reply_text": "试玩回复", "reason": "看着像有效提问"}],
                "summary": {"count": 1, "replied": 1}}

    ui.agent_playground = runner
    asyncio.run(ui._ag_playground({"events": [{"type": "chat", "user": "甲",
                                               "content": "在吗"}]}))
    assert calls[0] == {"events": [{"type": "chat", "user": "甲", "content": "在吗"}]}
    msg = out[-1]
    assert msg["ok"] is True and msg["data"]["ok"] is True
    assert msg["data"]["results"][0]["reason"] == "看着像有效提问"
    assert msg["data"]["summary"]["replied"] == 1


def test_ag_playground_passes_batch_through(logger_setup):
    ui = make_ui()
    sent_of(ui)
    calls = []

    async def runner(payload):
        calls.append(payload)
        return {"results": [], "summary": {}}

    ui.agent_playground = runner
    asyncio.run(ui._ag_playground({"batch": {"type": "chat", "count": 5, "content": "666"}}))
    assert calls == [{"batch": {"type": "chat", "count": 5, "content": "666"}}]


def test_ag_playground_runner_failure_reports_message(logger_setup):
    """表单非法（`PlaygroundError`）与上游异常都要变成人话，而不是让 WS 循环炸掉。"""
    ui = make_ui()
    out = sent_of(ui)

    async def runner(payload):
        raise ValueError("观众昵称不能为空")

    ui.agent_playground = runner
    asyncio.run(ui._ag_playground({"events": [{"type": "chat"}]}))
    msg = out[-1]
    assert msg["ok"] is False
    assert msg["data"]["message"] == "观众昵称不能为空"
    assert msg["data"]["results"] == []


# ---------------------------------------------------------------- 端到端


def test_m5_actions_over_websocket(logger_setup):
    ui = make_ui()
    ui.agent_logs_provider = lambda limit: {"available": True, "records": [rec()],
                                            "summary": {"total": 1}, "path": "p", "note": ""}

    async def runner(payload):
        return {"results": [{"index": 0, "should_reply": True, "reply_text": "试玩回复"}],
                "summary": {"count": 1}}

    ui.agent_playground = runner

    async def scenario():
        await ui.start()
        try:
            async with connect(f"ws://127.0.0.1:{ui.port}/ws") as ws:
                first = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                assert first["type"] == "snapshot"

                await ws.send(json.dumps({"action": "ag_logs"}))
                got = await _recv_type(ws, "agent_logs")
                assert got["data"]["records"][0]["reply_text"] == "点小手柄下载"

                await ws.send(json.dumps({"action": "ag_playground",
                                          "events": [{"type": "chat", "user": "甲",
                                                      "content": "在吗"}]}))
                got = await _recv_type(ws, "agent_playground")
                assert got["ok"] is True
                assert got["data"]["results"][0]["reply_text"] == "试玩回复"
        finally:
            await ui.close()

    asyncio.run(scenario())


async def _recv_type(ws, want: str) -> dict:
    for _ in range(20):
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
        if msg.get("type") == want:
            return msg
    raise AssertionError(f"未收到 {want} 消息")
