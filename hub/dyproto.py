"""抖音 webcast IM protobuf 编解码（协议移植自 Dycast 源码 src/core/model/）。

零第三方依赖：手写 proto wire-format 解码，只覆盖弹幕链路所需字段；
未知字段按 wire type 跳过，字段号变更或新增不致崩溃。

字段号来源（dycast-src/src/core/model/）：
  PushFrame   seqId=1 logId=2 service=3 method=4 headersList=5(payloadEncoding=6)
              payloadType=7 payload=8 lodIdNew=9
  Response    messages=1 cursor=2 fetchInterval=3 now=4 internalExt=5 fetchType=6
              heartbeatDuration=8 needAck=9 pushServer=10 liveCursor=11
  Message     method=1 payload=2 msgId=3 msgType=4
  ChatMessage user=2 content=3 rtfContentV2=41
  GiftMessage repeatCount=5 comboCount=6 user=7 toUser=8 repeatEnd=9 gift=15 count=44
  LikeMessage count=2 total=3 user=5
  MemberMessage user=2 memberCount=3
  SocialMessage user=2 followCount=6
  User        id=1 shortId=2 nickname=3 gender=4 avatarThumb=9 status=15 secUid=46
  GiftStruct  image=1 describe=2 id=5 type=11 diamondCount=12 icon=15 name=16
  Text        key=1 defaultPattern=2 pieces=4
  TextPiece   type=1 stringValue=11 giftValue=22 userValue=21 imageValue=25
  Image       urlList=1

输出与 Dycast 的 DyMessage dict 同构，hub.events.normalize 可直接消费。
"""
import gzip

# ── wire format 基元 ────────────────────────────────────────────────


class ProtoError(ValueError):
    """二进制不符合 proto wire format。"""


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise ProtoError("varint 越界")
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ProtoError("varint 超过 10 字节")


def _read_bytes_field(buf: bytes, pos: int) -> tuple[bytes, int]:
    length, pos = _read_varint(buf, pos)
    end = pos + length
    if end > len(buf):
        raise ProtoError("length-delimited 越界")
    return buf[pos:end], end


def _skip_field(buf: bytes, pos: int, wire: int) -> int:
    if wire == 0:
        _, pos = _read_varint(buf, pos)
        return pos
    if wire == 1:
        return pos + 8
    if wire == 2:
        _, pos = _read_bytes_field(buf, pos)
        return pos
    if wire == 5:
        return pos + 4
    raise ProtoError(f"不支持的 wire type {wire}")


def _decode_fields(buf: bytes):
    """yield (field_no, wire, value)；wire 0 → int，wire 2 → bytes。"""
    pos = 0
    while pos < len(buf):
        tag, pos = _read_varint(buf, pos)
        field_no, wire = tag >> 3, tag & 7
        if field_no == 0:
            raise ProtoError("field 0 非法")
        if wire == 0:
            value, pos = _read_varint(buf, pos)
        elif wire == 2:
            value, pos = _read_bytes_field(buf, pos)
        else:
            pos = _skip_field(buf, pos, wire)
            continue
        yield field_no, wire, value


def _write_varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _tag(field_no: int, wire: int) -> bytes:
    return _write_varint((field_no << 3) | wire)


def _write_str(field_no: int, text: str) -> bytes:
    raw = text.encode("utf-8")
    return _tag(field_no, 2) + _write_varint(len(raw)) + raw


def _write_bytes(field_no: int, data: bytes) -> bytes:
    return _tag(field_no, 2) + _write_varint(len(data)) + data


def _maybe_gunzip(payload: bytes, headers: dict) -> bytes:
    if headers.get("compress_type") == "gzip":
        return gzip.decompress(payload)
    return payload


# ── PushFrame：WS 二进制帧的传输外壳 ─────────────────────────────────


def decode_push_frame(data: bytes) -> dict:
    frame = {"headers": {}}
    for field_no, wire, value in _decode_fields(data):
        if field_no == 2 and wire == 0:
            frame["log_id"] = value
        elif field_no == 5 and wire == 2:
            key = value_key = value_val = None
            for k, w, v in _decode_fields(value):
                if k == 1 and w == 2:
                    value_key = v.decode("utf-8", "replace")
                elif k == 2 and w == 2:
                    value_val = v.decode("utf-8", "replace")
            if value_key is not None and value_val is not None:
                frame["headers"][value_key] = value_val
        elif field_no == 7 and wire == 2:
            frame["payload_type"] = value.decode("utf-8", "replace")
        elif field_no == 8 and wire == 2:
            frame["payload"] = value
    return frame


def encode_push_frame(payload_type: str = "", payload: bytes = b"", log_id: int = 0) -> bytes:
    """构造 hb / ack 帧（与 Dycast encodePushFrame 同口径）。"""
    out = bytearray()
    if log_id:
        out += _tag(2, 0) + _write_varint(log_id)
    if payload_type:
        out += _write_str(7, payload_type)
    if payload:
        out += _write_bytes(8, payload)
    return bytes(out)


# ── Response：解压后的消息集合 ──────────────────────────────────────


def decode_response(data: bytes) -> dict:
    res = {"messages": [], "need_ack": False}
    for field_no, wire, value in _decode_fields(data):
        if field_no == 1 and wire == 2:
            res["messages"].append(_decode_im_message(value))
        elif field_no == 2 and wire == 2:
            res["cursor"] = value.decode("utf-8", "replace")
        elif field_no == 5 and wire == 2:
            res["internal_ext"] = value.decode("utf-8", "replace")
        elif field_no == 8 and wire == 0:
            res["heartbeat_duration"] = value
        elif field_no == 9 and wire == 0:
            res["need_ack"] = value != 0
        elif field_no in (10, 13, 14) and wire == 2:
            res[{10: "push_server", 13: "proxy_server",
                 14: "push_server_v2"}[field_no]] = value.decode("utf-8", "replace")
    return res


def _decode_im_message(data: bytes) -> dict:
    msg = {}
    for field_no, wire, value in _decode_fields(data):
        if field_no == 1 and wire == 2:
            msg["method"] = value.decode("utf-8", "replace")
        elif field_no == 2 and wire == 2:
            msg["payload"] = value
        elif field_no == 3 and wire == 0:
            msg["msg_id"] = value
    return msg


# ── 各业务消息 → DyMessage dict（与 Dycast 输出同构） ─────────────────


def decode_user(data: bytes) -> dict:
    user = {}
    for field_no, wire, value in _decode_fields(data):
        if field_no == 1 and wire == 0:
            user["id"] = str(value)
        elif field_no == 3 and wire == 2:
            user["name"] = value.decode("utf-8", "replace")
        elif field_no == 4 and wire == 0:
            user["gender"] = value
        elif field_no == 9 and wire == 2:
            user["avatar"] = _decode_image_first_url(value)
        elif field_no == 46 and wire == 2:
            user["sec_uid"] = value.decode("utf-8", "replace")
    return user


def _decode_image_first_url(data: bytes) -> str:
    for field_no, wire, value in _decode_fields(data):
        if field_no == 1 and wire == 2:
            return value.decode("utf-8", "replace")
    return ""


def decode_gift_struct(data: bytes) -> dict:
    gift = {}
    for field_no, wire, value in _decode_fields(data):
        if field_no == 2 and wire == 2:
            gift["desc"] = value.decode("utf-8", "replace")
        elif field_no == 5 and wire == 0:
            gift["id"] = str(value)
        elif field_no == 11 and wire == 0:
            gift["type"] = value
        elif field_no == 12 and wire == 0:
            gift["price"] = value  # diamond_count（抖币），换算口径 OQ-1 单点在 normalize
        elif field_no == 15 and wire == 2:
            gift["icon"] = _decode_image_first_url(value)
        elif field_no == 16 and wire == 2:
            gift["name"] = value.decode("utf-8", "replace")
    return gift


def decode_text(data: bytes) -> str:
    """富文本 Text → 纯文本：拼接 stringValue 分片，表情/@ 记为名称占位。"""
    parts = []
    for field_no, wire, value in _decode_fields(data):
        if field_no != 4 or wire != 2:
            continue
        for k, w, v in _decode_fields(value):
            if k == 11 and w == 2:  # 普通文本
                parts.append(v.decode("utf-8", "replace"))
            elif k == 25 and w == 2:  # 合成表情：取 emoji 名称（TextPieceImage.image 无 name，空）
                pass
            elif k == 21 and w == 2:  # @用户
                for uk, uw, uv in _decode_fields(v):
                    if uk == 1 and uw == 2:
                        nickname = decode_user(uv).get("name")
                        if nickname:
                            parts.append(f"@{nickname}")
    return "".join(parts)


def decode_chat_message(payload: bytes) -> dict:
    """WebcastChatMessage → DyMessage。content 为空时回退 rtfContentV2 拼接。"""
    user = {}
    content = ""
    rtf = b""
    for field_no, wire, value in _decode_fields(payload):
        if field_no == 2 and wire == 2:
            user = decode_user(value)
        elif field_no == 3 and wire == 2:
            content = value.decode("utf-8", "replace")
        elif field_no == 41 and wire == 2:
            rtf = value
    if not content.strip() and rtf:
        content = decode_text(rtf)
    return {"method": "WebcastChatMessage", "user": user, "content": content}


def decode_gift_message(payload: bytes) -> dict:
    user = {}
    repeat_count = 0
    combo_count = 0
    repeat_end = 0
    gift = {}
    for field_no, wire, value in _decode_fields(payload):
        if field_no == 5 and wire == 0:
            repeat_count = value
        elif field_no == 6 and wire == 0:
            combo_count = value
        elif field_no == 7 and wire == 2:
            user = decode_user(value)
        elif field_no == 9 and wire == 0:
            repeat_end = value
        elif field_no == 15 and wire == 2:
            gift = decode_gift_struct(value)
        elif field_no == 44 and wire == 0:
            repeat_count = repeat_count or value
    gift["count"] = repeat_count or combo_count or 1
    gift["repeatEnd"] = repeat_end
    return {"method": "WebcastGiftMessage", "user": user, "gift": gift}


def decode_like_message(payload: bytes) -> dict:
    user = {}
    count = 0
    for field_no, wire, value in _decode_fields(payload):
        if field_no == 2 and wire == 0:
            count = value
        elif field_no == 5 and wire == 2:
            user = decode_user(value)
    return {"method": "WebcastLikeMessage", "user": user,
            "content": f"为主播点赞了({count})"}


def decode_member_message(payload: bytes) -> dict:
    user = {}
    member_count = 0
    for field_no, wire, value in _decode_fields(payload):
        if field_no == 2 and wire == 2:
            user = decode_user(value)
        elif field_no == 3 and wire == 0:
            member_count = value
    return {"method": "WebcastMemberMessage", "user": user, "content": "进入直播间",
            "room": {"audienceCount": member_count}}


def decode_social_message(payload: bytes) -> dict:
    user = {}
    follow_count = 0
    for field_no, wire, value in _decode_fields(payload):
        if field_no == 2 and wire == 2:
            user = decode_user(value)
        elif field_no == 6 and wire == 0:
            follow_count = value
    return {"method": "WebcastSocialMessage", "user": user, "content": "关注了主播",
            "room": {"followCount": follow_count}}


_PAYLOAD_DECODERS = {
    "WebcastChatMessage": decode_chat_message,
    "WebcastGiftMessage": decode_gift_message,
    "WebcastLikeMessage": decode_like_message,
    "WebcastMemberMessage": decode_member_message,
    "WebcastSocialMessage": decode_social_message,
}

# 与 hub.events.METHOD_TO_TYPE 白名单一致（like 不生成事件，仅日志层观察）
_EVENT_METHODS = {
    "WebcastChatMessage", "WebcastGiftMessage", "WebcastSocialMessage",
    "WebcastMemberMessage",
}


def response_dy_messages(response: dict) -> list[dict]:
    """已解码 Response 的 messages → DyMessage dict 列表（白名单过滤，供 im/fetch 初帧用）。

    每条附带帧层 msgId（dy["msg_id"]）供去重：cursor 续传与 ACK 延迟都会让
    服务端重发同一条消息。
    """
    out = []
    for msg in response.get("messages", []):
        method = msg.get("method")
        if method not in _EVENT_METHODS:
            continue
        try:
            dy = _PAYLOAD_DECODERS[method](msg["payload"])
        except (ProtoError, KeyError, ValueError):
            continue
        if msg.get("msg_id"):
            dy["msg_id"] = str(msg["msg_id"])
        out.append(dy)
    return out


def decode_frame_messages(data: bytes) -> list[dict]:
    """WS 二进制帧 → DyMessage dict 列表（传输层一步到位：含 gzip 与白名单过滤）。"""
    frame = decode_push_frame(data)
    payload = frame.get("payload")
    if payload is None:
        return []
    payload = _maybe_gunzip(payload, frame["headers"])
    return response_dy_messages(decode_response(payload))
