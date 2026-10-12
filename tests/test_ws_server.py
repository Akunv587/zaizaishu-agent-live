"""hub.ws_server 的单元测试（PROMPTS 步骤 1.2 / 1.3）。

验收要求：服务端 + websockets 客户端连接，发送直播间信息对象与混合数组
（含白名单内外多种 method 与一条损坏 JSON），断言回调收到正确条数且服务不中断。
回调链经 normalize 归一化，收到的是 LiveEvent。
"""
import asyncio
import json

import pytest
from websockets.asyncio.client import connect

from hub.ws_server import WsServer


@pytest.fixture
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "debug")
    return tmp_path


async def _start_server(on_event, clock=None):
    server = WsServer(on_event, "127.0.0.1:0", clock=clock)
    await server.start()
    return server


def test_mixed_batch_and_broken_json(logger_setup):
    received = []

    async def on_event(ev):
        received.append(ev)

    async def scenario():
        server = await _start_server(on_event)
        url = f"ws://127.0.0.1:{server.port}"
        try:
            async with connect(url) as ws:
                await ws.send(json.dumps({"roomNum": 12345, "nickname": "测试主播", "title": "游戏直播"}))
                await ws.send(json.dumps([
                    {"method": "WebcastChatMessage", "user": {"name": "观众A"}, "content": "主播这是什么游戏"},
                    {"method": "WebcastGiftMessage", "user": {"name": "观众B"},
                     "gift": {"name": "小心意", "count": 1, "price": 100, "repeatEnd": False}},
                    {"method": "WebcastLikeMessage", "count": 10},
                    {"method": "WebcastSocialMessage", "user": {"name": "观众C"}},
                ]))
                await ws.send("{broken json !!!")
                await ws.send(json.dumps([{"method": "WebcastMemberMessage", "user": {"name": "观众D"}}]))
                await asyncio.sleep(0.3)
        finally:
            await server.close()

    asyncio.run(scenario())

    types = [ev.type for ev in received]
    assert types == ["chat", "gift", "follow", "enter"]  # 白名单外不透传；损坏 JSON 不中断
    assert received[0].user == "观众A"
    assert received[1].gift_name == "小心意"
    assert received[1].combo_end is False


def test_reconnect_keeps_server_listening(logger_setup):
    received = []

    async def on_event(ev):
        received.append(ev)

    async def scenario():
        server = await _start_server(on_event)
        url = f"ws://127.0.0.1:{server.port}"
        try:
            async with connect(url) as ws1:  # 第一个连接：发一条后断开
                await ws1.send(json.dumps([{"method": "WebcastChatMessage", "user": {"name": "甲"}, "content": "第一条"}]))
                await asyncio.sleep(0.1)
            await asyncio.sleep(0.1)  # 断开缓冲
            async with connect(url) as ws2:  # 重连：仍能收
                await ws2.send(json.dumps([
                    {"method": "WebcastChatMessage", "user": {"name": "乙"}, "content": "重连后消息"},
                ]))
                await asyncio.sleep(0.2)
        finally:
            await server.close()

    asyncio.run(scenario())
    assert len(received) == 2
    assert received[1].content == "重连后消息"


def test_live_info_logged_and_fields_captured(logger_setup):
    async def on_event(ev):
        pass

    async def scenario():
        server = await _start_server(on_event)
        try:
            async with connect(f"ws://127.0.0.1:{server.port}") as ws:
                await ws.send(json.dumps({"roomNum": 998877, "nickname": "仔仔鼠", "title": "推游戏"}))
                await asyncio.sleep(0.2)
        finally:
            await server.close()

    asyncio.run(scenario())
    day = __import__("datetime").datetime.now().strftime("%Y%m%d")
    content = (logger_setup / f"live-{day}.log").read_text(encoding="utf-8")
    assert "live_info" in content
    assert "roomNum" in content


def test_single_dict_dymessage_is_emitted(logger_setup):
    """单对象形态的白名单消息应按事件处理（不误判为直播间信息对象）。"""
    received = []

    async def on_event(ev):
        received.append(ev)

    async def scenario():
        server = await _start_server(on_event)
        try:
            async with connect(f"ws://127.0.0.1:{server.port}") as ws:
                await ws.send(json.dumps({"method": "WebcastChatMessage",
                                          "user": {"name": "观众E"}, "content": "单对象弹幕"}))
                await ws.send(json.dumps({"method": "WebcastLikeMessage", "count": 3}))
                await asyncio.sleep(0.2)
        finally:
            await server.close()

    asyncio.run(scenario())
    assert [ev.type for ev in received] == ["chat"]  # 白名单外单对象忽略
    assert received[0].content == "单对象弹幕"
    day = __import__("datetime").datetime.now().strftime("%Y%m%d")
    lines = (logger_setup / f"live-{day}.log").read_text(encoding="utf-8").splitlines()
    live_info = [l for l in lines if '"event": "live_info"' in l]
    assert not any("单对象弹幕" in l for l in live_info)  # 未被误记为直播间信息


def test_sync_callback_and_injected_clock(logger_setup):
    received = []
    fake_time = iter([1000.0, 1000.5, 1001.0])

    def on_event(ev):  # 同步回调同样可用
        received.append(ev)

    async def scenario():
        server = await _start_server(on_event, clock=lambda: next(fake_time))
        try:
            async with connect(f"ws://127.0.0.1:{server.port}") as ws:
                await ws.send(json.dumps([
                    {"method": "WebcastFansclubMessage", "user": {"name": "团友"}, "content": "加入粉丝团"},
                    {"method": "WebcastChatMessage", "user": {"name": "路人"}, "content": "在吗"},
                ]))
                await asyncio.sleep(0.2)
        finally:
            await server.close()

    asyncio.run(scenario())
    assert received[0].type == "fansclub"
    assert received[1].type == "chat"
    assert received[0].ts == 1000.0
    assert received[1].ts == 1000.5
