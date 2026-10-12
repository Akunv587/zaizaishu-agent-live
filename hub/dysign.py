"""抖音 webcast 原生签名与 URL 构造（算法移植自 douyinLive v2.2.1，MIT）。

2026-09-03 实验验证：纯 Python 直连突破设备风控（旧 X-Bogus 链路被
DEVICE_BLOCKED 拦截），无需浏览器环境、无需登录态 cookie、无需 a_bogus。

组件：
  rc4()                — RC4 流加密（ws_sign 内部用）
  ws_sign()            — WebSocket 握手 16 位原生签名（突破风控的关键）
  xms_stub()           — 签名输入：room_id/user_unique_id 规范串的 MD5 摘要
  ms_token()           — 随机 msToken（接口兼容参数，服务端不校验）
  normalize_push_url() — im/fetch 下发的 push_server 规范化
  push_url_query()     — push/v2 WS 连接查询串（口径对齐 Go websocket_params.go）
  fetch_url_query()    — im/fetch 预取查询串

字段口径（douyinlive-proto / websocket_params.go）：
  签名覆盖 live_id,aid,version_code,webcast_sdk_version,room_id,sub_room_id,
  sub_channel_id,did_rule,user_unique_id,device_platform,device_type,ac,identity
  （逗号连接、按键序）；push URL 优先级 push_server_v2 > push_server > proxy_server。
"""
import base64
import hashlib
import random
import time
import urllib.parse

PUSH_PATH = "/webcast/im/push/v2/"
DEFAULT_PUSH_URL = "wss://webcast100-ws-web-lq.douyin.com" + PUSH_PATH
DEFAULT_CURSOR = "d-1_u-1_fh-7383731312643626035_t-1719159695790_r-1"
DEFAULT_WRDS_V = "7382620942951772256"
SDK_VERSION = "1.0.15"
VERSION_CODE = "180800"
AID = "6383"

_WS_ALPHABET = "Dkdpgh4ZKsQB80/Mfvw36XI1R25+WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe"
_EMPTY_DIGEST = hashlib.md5(hashlib.md5(b"").digest()).digest()
_counter = random.randint(0, 20)


def rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def xms_stub(room_id: str, user_unique_id: str) -> str:
    """签名输入：规范化参数串的 MD5（user_unique_id 传主播 pushID）。"""
    joined = ("live_id=1,aid=6383,version_code=180800,webcast_sdk_version=1.0.15,"
              f"room_id={room_id},sub_room_id=,sub_channel_id=,did_rule=3,"
              f"user_unique_id={user_unique_id},device_platform=web,device_type=,ac=,identity=audience")
    return hashlib.md5(joined.encode()).hexdigest()


def ws_sign(stub_hex: str) -> str:
    """16 位原生签名（douyinLive internal/webcastsign/native.go 同口径）。"""
    global _counter
    stub_digest = hashlib.md5(bytes.fromhex(stub_hex)).digest()
    flag_random = random.randrange(256)
    _counter = (_counter + 1) & 0x3F
    counter = _counter
    payload_random = random.randrange(255)
    key_random = random.randrange(255)

    payload = bytearray([counter & 0x3F, 0, 1, 14,
                         _EMPTY_DIGEST[14], _EMPTY_DIGEST[15],
                         stub_digest[14], stub_digest[15], payload_random, 0])
    checksum = 0
    for i in range(len(payload) - 1):
        checksum ^= payload[i]
    payload[9] = checksum

    encrypted = rc4(bytes([key_random]), bytes(payload))
    flags = 0x40 | (0x10 if flag_random & 1 else 0)
    result = bytes([flags, key_random]) + encrypted
    std = base64.b64encode(result).decode().rstrip("=")
    return std.translate(str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",
        _WS_ALPHABET))


def ms_token(length: int = 172) -> str:
    charset = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    return "".join(random.choice(charset) for _ in range(length))


def normalize_push_url(candidate: str) -> str:
    """push_server 字段 → 规范 wss URL（截断逗号/分号后缀、补 push/v2 路径）。"""
    candidate = str(candidate or "").strip()
    if not candidate:
        return ""
    idx = min((i for i in (candidate.find(","), candidate.find(";")) if i >= 0),
              default=-1)
    if idx >= 0:
        candidate = candidate[:idx].strip()
    candidate = candidate.rstrip("/")

    if candidate.startswith(("wss://", "ws://")):
        pass
    elif candidate.startswith("https://"):
        candidate = "wss://" + candidate[len("https://"):]
    elif candidate.startswith("http://"):
        candidate = "ws://" + candidate[len("http://"):]
    else:
        candidate = "wss://" + candidate

    if PUSH_PATH.rstrip("/") in candidate:
        return candidate + "/"
    return candidate + PUSH_PATH


def default_internal_ext(room_id: str, user_unique_id: str) -> str:
    now_ms = int(time.time() * 1000)
    return (f"internal_src:dim|wss_push_room_id:{room_id}"
            f"|wss_push_did:{user_unique_id}|first_req_ms:{now_ms}"
            f"|fetch_time:{now_ms}|seq:1|wss_info:0-{now_ms}-0-0"
            f"|wrds_v:{DEFAULT_WRDS_V}")


def _qv(value: str) -> str:
    return value.replace(" ", "%20")


def push_url_query(room_id: str, user_unique_id: str, cursor: str,
                   internal_ext: str, signature: str, browser_version: str,
                   screen: tuple[int, int] = (1920, 1080)) -> str:
    """push/v2 WS 连接查询串（字段与顺序对齐 Go websocket_params.go）。"""
    parts = [
        "app_name=douyin_web", f"version_code={VERSION_CODE}",
        f"webcast_sdk_version={SDK_VERSION}", f"update_version_code={SDK_VERSION}",
        "compress=gzip", "device_platform=web", "cookie_enabled=true",
        f"screen_width={screen[0]}", f"screen_height={screen[1]}",
        "browser_language=zh-CN", "browser_platform=Win32", "browser_name=Mozilla",
        "browser_version=" + _qv(browser_version), "browser_online=true",
        "tz_name=Asia/Shanghai", "cursor=" + cursor,
        "internal_ext=" + internal_ext, "host=https://live.douyin.com",
        f"aid={AID}", "live_id=1", "did_rule=3", "endpoint=live_pc",
        "support_wrds=1", "user_unique_id=" + user_unique_id,
        "im_path=/webcast/im/fetch/", "identity=audience",
        "need_persist_msg_count=15", "insert_task_id=", "live_reason=",
        "room_id=" + room_id, "heartbeatDuration=0",
        "signature=" + _qv(signature),
    ]
    return "&".join(parts)


def fetch_url_query(room_id: str, user_unique_id: str, browser_version: str,
                    token: str, screen: tuple[int, int] = (1920, 1080)) -> str:
    """im/fetch 预取查询串（resp_content_type=protobuf，cursor/internal_ext 起始为空）。"""
    qe = urllib.parse.quote
    parts = [
        "resp_content_type=protobuf", "did_rule=3", "device_id=",
        "app_name=douyin_web", "endpoint=live_pc", "support_wrds=1",
        "user_unique_id=" + qe(user_unique_id, safe=""), "identity=audience",
        "need_persist_msg_count=15", "insert_task_id=", "live_reason=",
        "room_id=" + qe(room_id, safe=""), f"version_code={VERSION_CODE}",
        "last_rtt=0", "live_id=1", f"aid={AID}", "fetch_rule=1",
        "cursor=", "internal_ext=", "device_platform=web",
        "cookie_enabled=true", f"screen_width={screen[0]}",
        f"screen_height={screen[1]}", "browser_language=zh-CN",
        "browser_platform=Win32", "browser_name=Mozilla",
        "browser_version=" + qe(browser_version, safe=""),
        "browser_online=true", "tz_name=Asia/Shanghai",
        "msToken=" + qe(token, safe=""),
    ]
    return "&".join(parts)
