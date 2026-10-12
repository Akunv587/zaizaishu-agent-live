"""内置弹幕采集器测试：protobuf 编解码 / 原生签名 / web/enter 解析 / 采集器会话。

覆盖协议移植正确性（douyinLive 字段号口径）与 DyMessage 归一化对接，
全部离线（httpx.MockTransport + 手工构造帧），真实连接由 scripts/douyin_probe.py 验证。
"""
import asyncio
import gzip

import httpx
import pytest

from hub import douyin as douyin_mod
from hub.douyin import (DouyinCollector, DouyinError, RECV_TIMEOUT_S,
                        _seen_before, extract_room_num, parse_enter_response)
from hub.dyproto import (ProtoError, decode_frame_messages, decode_push_frame,
                         decode_response, decode_user, encode_push_frame,
                         response_dy_messages, _tag, _write_varint)
from hub.dysign import (DEFAULT_CURSOR, DEFAULT_PUSH_URL, default_internal_ext,
                        fetch_url_query, normalize_push_url, push_url_query,
                        ws_sign, xms_stub)
from hub.events import LiveEvent, normalize

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/110.0.5481.77 Safari/537.36")


# ── 测试用 protobuf 构造基元 ────────────────────────────────────────


def f_str(n: int, s: str) -> bytes:
    raw = s.encode("utf-8")
    return _tag(n, 2) + _write_varint(len(raw)) + raw


def f_bytes(n: int, data: bytes) -> bytes:
    return _tag(n, 2) + _write_varint(len(data)) + data


def f_varint(n: int, v: int) -> bytes:
    return _tag(n, 0) + _write_varint(v)


def make_push_frame(payload: bytes, payload_type: str = "msg",
                    headers: dict = None, log_id: int = 0) -> bytes:
    out = b""
    if log_id:
        out += f_varint(2, log_id)
    for k, v in (headers or {}).items():
        out += f_bytes(5, f_str(1, k) + f_str(2, v))
    out += f_str(7, payload_type)
    out += f_bytes(8, payload)
    return out


def make_chat_msg(user_name: str, content: str) -> bytes:
    """构造 Response 二进制：messages=[Message{method, payload=ChatMessage}]。"""
    chat = f_bytes(2, f_str(3, user_name)) + f_str(3, content)
    im_msg = f_str(1, "WebcastChatMessage") + f_bytes(2, chat)
    return f_bytes(1, im_msg)


def make_gift_msg(user_name: str, gift_name: str, count: int,
                  price: int, repeat_end: int) -> bytes:
    gift_struct = (f_str(2, "礼物描述") + f_varint(5, 9001)
                   + f_varint(12, price) + f_str(16, gift_name))
    gift_msg = (f_varint(5, count) + f_bytes(7, f_str(3, user_name))
                + f_varint(9, repeat_end) + f_bytes(15, gift_struct))
    im_msg = f_str(1, "WebcastGiftMessage") + f_bytes(2, gift_msg)
    return f_bytes(1, im_msg)


# ── PushFrame 编解码 ────────────────────────────────────────────────


class TestPushFrame:
    def test_hb_roundtrip(self):
        hb = encode_push_frame(payload_type="hb")
        frame = decode_push_frame(hb)
        assert frame["payload_type"] == "hb"
        assert "payload" not in frame

    def test_ack_roundtrip(self):
        ack = encode_push_frame(payload_type="ack", payload="ext-data".encode(),
                                log_id=123456789)
        frame = decode_push_frame(ack)
        assert frame["payload_type"] == "ack"
        assert frame["log_id"] == 123456789
        assert frame["payload"] == b"ext-data"

    def test_headers_and_gzip_flag(self):
        frame_bin = make_push_frame(b"x", headers={"compress_type": "gzip",
                                                   "im-cursor": "cur-1"})
        frame = decode_push_frame(frame_bin)
        assert frame["headers"]["compress_type"] == "gzip"
        assert frame["headers"]["im-cursor"] == "cur-1"

    def test_unknown_field_skipped(self):
        # field 20 wire 2 未知字段 → 跳过后仍能解出 payloadType
        raw = f_bytes(20, b"unknown") + f_str(7, "hb")
        assert decode_push_frame(raw)["payload_type"] == "hb"

    def test_truncated_varint_raises(self):
        with pytest.raises(ProtoError):
            decode_push_frame(b"\x08")  # varint 未终止


# ── Response / 消息解码 ─────────────────────────────────────────────


class TestResponse:
    def test_messages_and_need_ack(self):
        res_bin = make_chat_msg("小明", "主播666") + f_varint(9, 1) + f_str(2, "cursor-abc")
        res = decode_response(res_bin)
        assert res["need_ack"] is True
        assert res["cursor"] == "cursor-abc"
        assert len(res["messages"]) == 1
        assert res["messages"][0]["method"] == "WebcastChatMessage"

    def test_push_server_fields(self):
        res = decode_response(f_str(14, "wss://push-v2.example.com/webcast/im/push/v2")
                              + f_str(10, "wss://push.example.com,backup")
                              + f_str(13, "https://proxy.example.com"))
        assert res["push_server_v2"] == "wss://push-v2.example.com/webcast/im/push/v2"
        assert res["push_server"] == "wss://push.example.com,backup"
        assert res["proxy_server"] == "https://proxy.example.com"

    def test_decode_frame_messages_chat(self):
        res_bin = make_chat_msg("小明", "主播666")
        raw = make_push_frame(res_bin)
        dys = decode_frame_messages(raw)
        assert dys == [{"method": "WebcastChatMessage", "user": {"name": "小明"},
                        "content": "主播666"}]

    def test_response_dy_messages(self):
        res_bin = make_chat_msg("小明", "初帧弹幕")
        dys = response_dy_messages(decode_response(res_bin))
        assert dys[0]["content"] == "初帧弹幕"

    def test_decode_frame_messages_gzip(self):
        res_bin = make_chat_msg("小明", "压缩弹幕")
        gz = gzip.compress(res_bin)
        raw = make_push_frame(gz, headers={"compress_type": "gzip"})
        dys = decode_frame_messages(raw)
        assert dys[0]["content"] == "压缩弹幕"

    def test_decode_frame_messages_gift(self):
        res_bin = make_gift_msg("老板", "嘉年华", 3, 3000, 1)
        dys = decode_frame_messages(make_push_frame(res_bin))
        dy = dys[0]
        assert dy["method"] == "WebcastGiftMessage"
        assert dy["user"]["name"] == "老板"
        assert dy["gift"]["name"] == "嘉年华"
        assert dy["gift"]["count"] == 3
        assert dy["gift"]["price"] == 3000
        assert dy["gift"]["repeatEnd"] == 1

    def test_non_event_method_filtered(self):
        # Like 消息解码但不进事件白名单
        like = f_varint(2, 5) + f_bytes(5, f_str(3, "点赞用户"))
        im_msg = f_str(1, "WebcastLikeMessage") + f_bytes(2, like)
        dys = decode_frame_messages(make_push_frame(f_bytes(1, im_msg)))
        assert dys == []

    def test_empty_payload_frame(self):
        assert decode_frame_messages(make_push_frame(b"")) == []

    def test_user_fields(self):
        user_bin = (f_varint(1, 123) + f_str(3, "昵称") + f_varint(4, 1)
                    + f_bytes(9, f_bytes(1, b"http://avatar"))
                    + f_str(46, "sec-uid-x"))
        user = decode_user(user_bin)
        assert user["id"] == "123"
        assert user["name"] == "昵称"
        assert user["gender"] == 1
        assert user["avatar"] == "http://avatar"
        assert user["sec_uid"] == "sec-uid-x"


# ── msg_id 透传与消息去重（cursor 续传/ACK 延迟重发 → "说两次"悬案） ──


def make_chat_msg_with_id(user_name: str, content: str, msg_id: int) -> bytes:
    """构造带帧层 msgId（Message field 3 varint）的 Response 二进制。"""
    chat = f_bytes(2, f_str(3, user_name)) + f_str(3, content)
    im_msg = f_str(1, "WebcastChatMessage") + f_bytes(2, chat) + f_varint(3, msg_id)
    return f_bytes(1, im_msg)


class TestMsgIdDedup:
    @pytest.fixture(autouse=True)
    def clear_seen_ids(self):
        douyin_mod._SEEN_MSG_IDS.clear()  # 进程级 LRU，用例间必须隔离
        yield
        douyin_mod._SEEN_MSG_IDS.clear()

    def test_response_dy_messages_attaches_msg_id(self):
        dys = response_dy_messages(
            decode_response(make_chat_msg_with_id("小明", "带ID弹幕", 987654321)))
        assert dys[0]["msg_id"] == "987654321"

    def test_frame_path_also_attaches_msg_id(self):
        raw = make_push_frame(make_chat_msg_with_id("小明", "帧弹幕", 12345))
        dys = decode_frame_messages(raw)
        assert dys[0]["msg_id"] == "12345"

    def test_seen_before_basic(self):
        assert _seen_before("m-1") is False   # 首次见登记
        assert _seen_before("m-1") is True    # 重复
        assert _seen_before("m-2") is False

    def test_seen_before_empty_id_passes_through(self):
        # 无 msg_id 的消息不猜、不去重，放行（宁可重不可丢）
        assert _seen_before("") is False
        assert _seen_before("") is False

    def test_seen_before_lru_eviction(self, monkeypatch):
        monkeypatch.setattr(douyin_mod, "_SEEN_MSG_IDS_CAP", 3)
        for mid in ("a", "b", "c", "d"):      # 容量 3 → "a" 被挤出
            assert _seen_before(mid) is False
        assert _seen_before("a") is False     # 已淘汰，视为新消息
        assert _seen_before("d") is True      # 仍在窗口内

    def test_dispatch_drops_duplicate_msg_id(self):
        events = []
        collector = DouyinCollector("8888", events.append)
        dy = {"method": "WebcastChatMessage", "user": {"name": "小明"},
              "content": "你好", "msg_id": "777"}
        collector._dispatch(dy)
        collector._dispatch(dict(dy))                       # 同 msg_id 重发 → 丢
        collector._dispatch({**dy, "msg_id": "778"})        # 新 ID → 过
        collector._dispatch({"method": "WebcastChatMessage",
                             "user": {"name": "小明"}, "content": "无ID"})
        collector._dispatch({"method": "WebcastChatMessage",
                             "user": {"name": "小明"}, "content": "无ID"})
        assert [e["content"] for e in events] == ["你好", "你好", "无ID", "无ID"]

    def test_recv_timeout_generous_for_quiet_rooms(self):
        # 冷清房间几十秒无帧是常态；协议层 ping/pong 才是死连接主判据，
        # 应用层阈值必须足够宽（曾以 25s 误杀导致反复断连重连）
        assert RECV_TIMEOUT_S >= 120


# ── DyMessage → LiveEvent 归一化对接 ─────────────────────────────────


class TestNormalizeBridge:
    def test_chat_to_live_event(self):
        dy = {"method": "WebcastChatMessage", "user": {"name": "小明"},
              "content": "主播666"}
        ev = normalize(dy, ts=1000.0)
        assert ev == LiveEvent(type="chat", user="小明", content="主播666", ts=1000.0)

    def test_gift_to_live_event(self):
        dy = {"method": "WebcastGiftMessage", "user": {"name": "老板"},
              "gift": {"name": "嘉年华", "count": 3, "price": 3000, "repeatEnd": 1}}
        ev = normalize(dy, ts=1000.0)
        assert ev.type == "gift"
        assert ev.gift_name == "嘉年华"
        assert ev.gift_count == 3
        assert ev.gift_value == 3000
        assert ev.combo_end is True

    def test_follow_to_live_event(self):
        dy = {"method": "WebcastSocialMessage", "user": {"name": "新粉"}}
        ev = normalize(dy)
        assert ev.type == "follow"

    def test_protobuf_full_chain(self):
        """构造帧 → decode_frame_messages → normalize 全链路。"""
        raw = make_push_frame(make_chat_msg("全链路", "真实弹幕"))
        ev = normalize(decode_frame_messages(raw)[0])
        assert (ev.type, ev.user, ev.content) == ("chat", "全链路", "真实弹幕")


# ── 原生签名（dysign） ──────────────────────────────────────────────


class TestDysign:
    def test_ws_sign_length_and_charset(self):
        sig = ws_sign(xms_stub("7418827158559333378", "105227539602"))
        assert len(sig) == 16
        allowed = set("Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe")
        assert set(sig) <= allowed

    def test_ws_sign_rotates(self):
        stub = xms_stub("123", "456")
        first = ws_sign(stub)
        second = ws_sign(stub)
        assert first != second

    def test_xms_stub_deterministic(self):
        assert xms_stub("123", "456") == xms_stub("123", "456")
        assert xms_stub("123", "456") != xms_stub("456", "123")

    def test_normalize_push_url_variants(self):
        assert (normalize_push_url("wss://a.com/webcast/im/push/v2")
                == "wss://a.com/webcast/im/push/v2/")
        assert (normalize_push_url("wss://a.com/webcast/im/push/v2/,x")
                == "wss://a.com/webcast/im/push/v2/")
        assert (normalize_push_url("https://a.com")
                == "wss://a.com/webcast/im/push/v2/")
        assert (normalize_push_url("http://a.com")
                == "ws://a.com/webcast/im/push/v2/")
        assert (normalize_push_url("a.douyin.com")
                == "wss://a.douyin.com/webcast/im/push/v2/")
        assert normalize_push_url("") == ""
        assert normalize_push_url(None) == ""

    def test_push_url_query_params(self):
        q = push_url_query("123", "456", "cur-1", "ext-1", "sig-abc",
                           UA[len("Mozilla/"):])
        parts = dict(p.split("=", 1) for p in q.split("&"))
        assert parts["app_name"] == "douyin_web"
        assert parts["compress"] == "gzip"
        assert parts["room_id"] == "123"
        assert parts["user_unique_id"] == "456"
        assert parts["cursor"] == "cur-1"
        assert parts["internal_ext"] == "ext-1"
        assert parts["signature"] == "sig-abc"
        assert parts["heartbeatDuration"] == "0"
        assert " " not in q

    def test_fetch_url_query_params(self):
        q = fetch_url_query("123", "456", UA[len("Mozilla/"):], "tok")
        parts = dict(p.split("=", 1) for p in q.split("&"))
        assert parts["resp_content_type"] == "protobuf"
        assert parts["room_id"] == "123"
        assert parts["user_unique_id"] == "456"
        assert parts["msToken"] == "tok"
        assert parts["cursor"] == ""

    def test_default_internal_ext(self):
        ext = default_internal_ext("123", "456")
        assert "wss_push_room_id:123" in ext
        assert "wss_push_did:456" in ext


# ── web/enter 解析 ──────────────────────────────────────────────────


ENTER_JSON = (
    '{"status_code":0,"data":{"data":[{"id_str":"7418827158559333378",'
    '"title":"测试直播间","status":2,"owner_user_id_str":"105227539602"}],'
    '"user":{"id_str":"105227539602","nickname":"测试主播"}}}'
)


class TestParseEnterResponse:
    def test_full_response(self):
        info = parse_enter_response(ENTER_JSON)
        assert info["room_id"] == "7418827158559333378"
        assert info["push_id"] == "105227539602"
        assert info["status"] == 2
        assert info["title"] == "测试直播间"
        assert info["nickname"] == "测试主播"

    def test_owner_fallback(self):
        body = ('{"data":{"room":{"id_str":"111","title":"t","status":2,'
                 '"owner":{"id_str":"222","nickname":"主播"}}}}')
        info = parse_enter_response(body)
        assert info["room_id"] == "111"
        assert info["push_id"] == "222"
        assert info["nickname"] == "主播"

    def test_missing_room(self):
        info = parse_enter_response('{"data":{}}')
        assert info["room_id"] == ""
        assert info["push_id"] == ""

    def test_invalid_json(self):
        info = parse_enter_response("<html>not json</html>")
        assert info["room_id"] == ""
        assert info["status"] == 0
        assert info["prompts"] == ""

    def test_prompts_visibility_restriction(self):
        """主播设可见范围时 prompts 带出原因（不再只报笼统错误）。"""
        body = '{"data":{"prompts":"你不在主播设置的可见范围内"},"status_code":4003034}'
        info = parse_enter_response(body)
        assert info["room_id"] == ""
        assert info["prompts"] == "你不在主播设置的可见范围内"

    def test_extract_room_num(self):
        assert extract_room_num("252873665935") == "252873665935"
        assert extract_room_num(" https://live.douyin.com/252873665935 ") == "252873665935"
        assert extract_room_num("") == ""


# ── 采集器（HTTP mock + 会话） ──────────────────────────────────────


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(data)


def make_client(enter_body: str = ENTER_JSON, enter_status: int = 200,
                fetch_body: bytes = b""):
    """MockTransport：web/enter 返回 JSON，im/fetch 返回 protobuf，其余 404。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/webcast/room/web/enter/":
            return httpx.Response(enter_status, text=enter_body,
                                  headers={"set-cookie": "ttwid=fake-ttwid; Path=/"})
        if path == "/webcast/im/fetch/":
            return httpx.Response(200, content=fetch_body)
        if path == "/":
            return httpx.Response(200, text="home",
                                  headers={"set-cookie": "ttwid=fake-ttwid; Path=/"})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                              base_url="https://live.douyin.com")


IM_FETCH_BODY = (f_str(2, "d-1_u-1_fh-1_t-1_r-1")
                 + f_str(5, "internal_src:dim|wss_push_room_id:7418827158559333378")
                 + f_str(10, "wss://push-mock.douyin.com/webcast/im/push/v2/")
                 + make_chat_msg("初帧用户", "初帧弹幕"))


class TestDouyinCollector:
    def test_empty_room_num_rejected(self):
        with pytest.raises(DouyinError):
            DouyinCollector("", lambda dy: None)

    def test_room_num_from_url(self):
        c = DouyinCollector("https://live.douyin.com/8888", lambda dy: None)
        assert c.room_num == "8888"

    def test_fetch_room_info_ok(self):
        async def run():
            c = DouyinCollector("8888", lambda dy: None)
            async with make_client() as client:
                await c._fetch_room_info(client)
            return c
        c = asyncio.run(run())
        assert c.room_id == "7418827158559333378"
        assert c.push_id == "105227539602"
        assert c.anchor == "测试主播"
        assert c.room_status == 2

    def test_fetch_room_info_http_error(self):
        async def run():
            c = DouyinCollector("8888", lambda dy: None)
            async with make_client(enter_status=503) as client:
                await c._fetch_room_info(client)
        with pytest.raises(DouyinError):
            asyncio.run(run())

    def test_fetch_room_info_missing_push_id(self):
        async def run():
            c = DouyinCollector("8888", lambda dy: None)
            async with make_client(enter_body='{"data":{"data":[{"id_str":"111"}]}}') as client:
                await c._fetch_room_info(client)
        with pytest.raises(DouyinError):
            asyncio.run(run())

    def test_fetch_im_info_extracts_fields_and_dispatches(self):
        received = []

        async def run():
            c = DouyinCollector("8888", received.append)
            c.room_id = "7418827158559333378"
            c.push_id = "105227539602"
            async with make_client(fetch_body=IM_FETCH_BODY) as client:
                return await c._fetch_im_info(client)
        info = asyncio.run(run())
        assert info["cursor"] == "d-1_u-1_fh-1_t-1_r-1"
        assert "wss_push_room_id:7418827158559333378" in info["internal_ext"]
        assert info["push_url"] == "wss://push-mock.douyin.com/webcast/im/push/v2/"
        assert len(received) == 1  # im/fetch 初帧消息也分发
        assert received[0]["content"] == "初帧弹幕"

    def test_fetch_im_info_fallback_on_error(self):
        async def run():
            c = DouyinCollector("8888", lambda dy: None)
            c.room_id = "123"
            c.push_id = "456"
            async with make_client(fetch_body=b"") as client:
                return await c._fetch_im_info(client)
        info = asyncio.run(run())
        assert info["cursor"] == DEFAULT_CURSOR
        assert info["push_url"] == DEFAULT_PUSH_URL
        assert "wss_push_room_id:123" in info["internal_ext"]

    def test_handle_frame_dispatch_and_ack(self):
        async def run():
            received = []
            c = DouyinCollector("8888", received.append)
            ws = FakeWs()
            res_bin = make_chat_msg("小明", "主播666") + f_varint(9, 1) + f_str(5, "ext-99")
            im_info = {"cursor": "", "internal_ext": "", "push_url": DEFAULT_PUSH_URL}
            new_info = await c._handle_frame(ws, make_push_frame(res_bin), im_info)
            return received, ws, new_info
        received, ws, new_info = asyncio.run(run())
        assert len(received) == 1
        assert received[0]["content"] == "主播666"
        assert len(ws.sent) == 1  # needAck → 回 ack
        ack = decode_push_frame(ws.sent[0])
        assert ack["payload_type"] == "ack"
        assert ack["payload"] == b"ext-99"
        assert new_info["internal_ext"] == "ext-99"

    def test_handle_frame_updates_push_url(self):
        async def run():
            c = DouyinCollector("8888", lambda dy: None)
            res_bin = f_str(14, "wss://new-push.douyin.com/webcast/im/push/v2")
            im_info = {"cursor": "", "internal_ext": "", "push_url": DEFAULT_PUSH_URL}
            return await c._handle_frame(FakeWs(), make_push_frame(res_bin), im_info)
        new_info = asyncio.run(run())
        assert new_info["push_url"] == "wss://new-push.douyin.com/webcast/im/push/v2/"

    def test_handle_frame_close(self):
        import websockets

        async def run():
            c = DouyinCollector("8888", lambda dy: None)
            raw = make_push_frame(b"", payload_type="close")
            await c._handle_frame(FakeWs(), raw, {"cursor": "", "internal_ext": "", "push_url": DEFAULT_PUSH_URL})
        with pytest.raises(websockets.ConnectionClosed):
            asyncio.run(run())

    def test_collect_round_wait_living(self):
        """主播未开播 → wait_living 状态并轮询返回（不连接 WS）。"""
        offline = ENTER_JSON.replace('"status":2', '"status":4')

        async def run():
            c = DouyinCollector("8888", lambda dy: None)
            stop = asyncio.Event()
            async with make_client(enter_body=offline) as client:
                await c._collect_round(client, stop)
            return c
        c = asyncio.run(run())
        assert c.state == "wait_living"

    def test_status_snapshot(self):
        c = DouyinCollector("8888", lambda dy: None)
        s = c.status()
        assert s["mode"] == "builtin"
        assert s["room_num"] == "8888"
        assert s["state"] == "idle"

    def test_external_cookie_priority(self):
        """粘贴的完整 cookie 优先于脚本自取的 ttwid（登录态是风控信任来源）。"""
        c = DouyinCollector("8888", lambda dy: None,
                            cookie="ttwid=real; sessionid=abc; msToken=xyz")
        c._ttwid = "script-ttwid"
        assert c._cookie_header() == "ttwid=real; sessionid=abc; msToken=xyz"
        assert c.status()["cookie"] is True

    def test_cookie_fallback_to_ttwid(self):
        """未粘贴 cookie 时回退首页 ttwid 口径。"""
        c = DouyinCollector("8888", lambda dy: None)
        assert c.status()["cookie"] is False
        c._ttwid = "script-ttwid"
        assert c._cookie_header() == "ttwid=script-ttwid"

    def test_cookie_paste_cleaned(self):
        """从开发者工具复制带入的换行/首尾空白被清理。"""
        c = DouyinCollector("8888", lambda dy: None,
                            cookie="  ttwid=a;\r\n sessionid=b; \n")
        assert c.cookie == "ttwid=a; sessionid=b;"

    def test_base_headers_with_cookie(self):
        """web/enter/im/fetch 请求头携带外部 cookie（与真实浏览器一致的登录态请求）。"""
        c = DouyinCollector("8888", lambda dy: None, cookie="ttwid=real")
        headers = c._base_headers()
        assert headers["Cookie"] == "ttwid=real"
        assert headers["Referer"] == "https://live.douyin.com/"

    def test_base_headers_without_any_cookie(self):
        """无外部 cookie 且 ttwid 未取到时不发空 Cookie 头。"""
        c = DouyinCollector("8888", lambda dy: None)
        assert "Cookie" not in c._base_headers()

    def test_netscape_cookie_file_converted(self):
        """cookies.txt 插件导出（Tab 分隔 7 字段）自动转 k=v; 串。"""
        raw = (
            "# Netscape HTTP Cookie File\n"
            "# This is a generated file! Do not edit.\n"
            ".www.douyin.com\tFALSE\t/\tFALSE\t1793199554\ts_v_web_id\tverify_x\n"
            "#HttpOnly_.douyin.com\tTRUE\t/\tTRUE\t1822575555\tsessionid\tabc123\n"
            ".www.douyin.com\tFALSE\t/\tFALSE\t0\t\t\n"
            ".www.douyin.com\tFALSE\t/\tFALSE\t0\tdevice_web_cpu_core\t12\n"
        )
        c = DouyinCollector("8888", lambda dy: None, cookie=raw)
        assert c.cookie == ("s_v_web_id=verify_x; sessionid=abc123; "
                            "device_web_cpu_core=12")

    def test_cookie_merged_with_ttwid_when_missing(self):
        """粘贴的 cookie 不含 ttwid → 补上首页取的（WS 握手必需）。"""
        c = DouyinCollector("8888", lambda dy: None, cookie="fpk1=x; fpk2=y")
        c._ttwid = "real-ttwid"
        assert c._cookie_header() == "fpk1=x; fpk2=y; ttwid=real-ttwid"

    def test_cookie_not_merged_when_ttwid_present(self):
        """粘贴的 cookie 已含 ttwid → 原样使用，不重复拼接。"""
        c = DouyinCollector("8888", lambda dy: None, cookie="ttwid=mine; fpk1=x")
        c._ttwid = "other"
        assert c._cookie_header() == "ttwid=mine; fpk1=x"
