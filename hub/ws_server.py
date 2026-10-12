"""WS 服务端：接收 Dycast Desktop 转发的结构化弹幕（PROMPTS 步骤 1.2）。

协议（DESIGN §4.1/§7.1）：
- 中枢为服务端，Dycast 为客户端，连接建立后先发一次直播间信息对象（非数组）
- 后续持续发 DyMessage JSON 数组，一次 message 是一个数组、可混合多种 method
- 只透传五类白名单消息，经 normalize 归一化后交给回调 on_event(LiveEvent)
- 容错：单条解析异常不中断；客户端断开后继续监听等待重连
"""
import asyncio
import inspect
import json
import time

from websockets.asyncio.server import serve

from hub.events import LiveEvent, normalize
from hub.logger import get_logger

METHOD_WHITELIST = {
    "WebcastChatMessage",
    "WebcastGiftMessage",
    "WebcastSocialMessage",
    "WebcastFansclubMessage",
    "WebcastMemberMessage",
}


class WsServer:
    """弹幕 WS 服务端。on_event 为注入回调（同步或异步均可），收 LiveEvent。"""

    def __init__(self, on_event, listen: str = "127.0.0.1:8765", log=None, clock=None):
        host, _, port = listen.rpartition(":")
        self._host = host or "127.0.0.1"
        self._port = int(port)
        self._on_event = on_event
        self._clock = clock or time.time
        self._log = log or get_logger("ws_server")
        self._server = None
        self._clients = 0

    @property
    def clients_connected(self) -> int:
        """当前接入的 Dycast 客户端数（控制台快照用）。"""
        return self._clients

    @property
    def port(self) -> int:
        if self._server is not None and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return self._port

    async def start(self):
        """启动监听并返回 server 对象（端口已在构造参数中指定）。"""
        self._server = await serve(self._handle_connection, self._host, self._port)
        self._log.info("ws_server_started", {"listen": f"{self._host}:{self._port}"})
        return self._server

    async def serve_forever(self):
        await self._server.serve_forever()

    async def close(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._log.info("ws_server_stopped")

    async def _handle_connection(self, connection):
        peer = getattr(connection, "remote_address", None)
        self._clients += 1
        self._log.info("client_connected", {"remote": str(peer)})
        got_live_info = False
        try:
            async for raw in connection:
                got_live_info = await self._process_message(raw, got_live_info)
        except Exception as e:  # 客户端断开等连接级异常
            self._log.info("client_disconnected", {"remote": str(peer), "reason": f"{type(e).__name__}: {e}"})
        finally:
            self._clients -= 1

    async def _process_message(self, raw, got_live_info: bool) -> bool:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            self._log.warning("json_parse_error", {"error": str(e), "raw_prefix": str(raw)[:120]})
            return got_live_info

        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    self._log.warning("non_dict_item_in_array", {"item": str(item)[:120]})
                    continue
                method = str(item.get("method") or "")
                if method in METHOD_WHITELIST:
                    await self._emit(item)
                else:
                    self._log.debug("method_ignored", {"method": method})
        elif isinstance(data, dict):
            # 单对象形态：有白名单 method 的是 DyMessage（宽容处理，与 replay 一致），
            # 无 method 才是直播间信息对象（记录全部字段，OQ-5 关闭依据）
            method = str(data.get("method") or "")
            if method in METHOD_WHITELIST:
                await self._emit(data)
            elif method:
                self._log.debug("method_ignored", {"method": method})
            else:
                self._log.info("live_info", {"fields": data})
                got_live_info = True
        else:
            self._log.warning("unknown_message_type", {"type": type(data).__name__})
        return got_live_info

    async def _emit(self, item: dict):
        ev = normalize(item, ts=self._clock())
        if ev is None:
            return  # normalize 内部已按情况记 warning 或 debug
        result = self._on_event(ev)
        if inspect.isawaitable(result):
            await result
