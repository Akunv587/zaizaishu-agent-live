"""X-Bogus 签名（纯 Python 移植，无 JS 引擎依赖）。

WS 连接 URL 的 signature 参数口径与 Dycast getSignature 一致：
先对固定顺序的连接参数串取 MD5 得 X-MS-STUB，再对 "X-MS-STUB=<md5hex>"
执行 X-Bogus 算法。算法步骤（社区公开逆向，与 byted_acrawler.frontierSign 对齐）：
  1. query 双重 MD5；POST body 双重 MD5（WS 场景为空串）
  2. UA 经 RC4(key=[0x00,0x01,0x0E]) 加密后 base64 再 MD5
  3. 19 字节数组 + XOR 校验位，奇偶重排
  4. RC4(key='ÿ') 加密后按自定义字符表 base64 变体编码
"""
import base64
import functools
import hashlib
import time

_X_BOGUS_SHORT_STR = "Dkdpgh4ZKsQB80/Mfvw36XI1R25-WUAlEi7NLboqYTOPuzmFjJnryx9HVGcaStCe="
_X_BOGUS_UA_KEY = ["\x00", "\x01", "\x0e"]


def _rc4(key: str, data: str) -> bytearray:
    d = list(range(256))
    c = 0
    for i in range(256):
        c = (c + d[i] + ord(key[i % len(key)])) % 256
        d[i], d[c] = d[c], d[i]
    t = 0
    c = 0
    ret = bytearray(len(data))
    for i in range(len(data)):
        t = (t + 1) % 256
        c = (c + d[t]) % 256
        d[t], d[c] = d[c], d[t]
        ret[i] = ord(data[i]) ^ d[(d[t] + d[c]) % 256]
    return ret


def _get_arr2(payload: str, ua: str, form: str) -> list:
    salt_payload = list(hashlib.md5(hashlib.md5(payload.encode()).digest()).digest())
    salt_form = list(hashlib.md5(hashlib.md5(form.encode()).digest()).digest())
    salt_ua = list(hashlib.md5(base64.b64encode(_rc4(_X_BOGUS_UA_KEY, ua))).digest())
    ts = int(time.time())
    arr1 = [
        64, 0, 1, 14,
        salt_payload[14], salt_payload[15],
        salt_form[14], salt_form[15],
        salt_ua[14], salt_ua[15],
        (ts >> 24) & 255, (ts >> 16) & 255, (ts >> 8) & 255, ts & 255,
        88, 194, 176, 26,
    ]
    arr1.append(functools.reduce(lambda a, b: a ^ b, arr1))
    return arr1[::2] + arr1[1::2]


def _get_garbled_string(arr2: list) -> list:
    p = [
        arr2[0], arr2[10], arr2[1], arr2[11], arr2[2], arr2[12], arr2[3], arr2[13],
        arr2[4], arr2[14], arr2[5], arr2[15], arr2[6], arr2[16], arr2[7], arr2[17],
        arr2[8], arr2[18], arr2[9],
    ]
    return [2, 255] + list(_rc4("\xff", "".join(chr(i) for i in p)))


def x_bogus(query: str, user_agent: str) -> str:
    """对 query string 计算 X-Bogus（长度 28）。"""
    arr2 = _get_arr2(query, user_agent, "")
    garbled = _get_garbled_string(arr2)
    ret = ""
    for i in range(0, 21, 3):
        base_num = garbled[i] << 16 | garbled[i + 1] << 8 | garbled[i + 2]
        for j in range(18, -1, -6):
            ret += _X_BOGUS_SHORT_STR[(base_num >> j) & 63]
    return ret


# webcast_sdk_version 与 Dycast VERSION 对齐
SDK_VERSION = "1.0.15"


def get_signature(room_id: str, unique_id: str, user_agent: str) -> str:
    """WS 连接 signature：Dycast getSignature 同口径的 X-Bogus。"""
    param_str = (
        f"live_id=1,aid=6383,version_code=180800,webcast_sdk_version={SDK_VERSION},"
        f"room_id={room_id},sub_room_id=,sub_channel_id=,did_rule=3,"
        f"user_unique_id={unique_id},device_platform=web,device_type=,ac=,identity=audience"
    )
    stub = hashlib.md5(param_str.encode()).hexdigest()
    return x_bogus(f"X-MS-STUB={stub}", user_agent)
