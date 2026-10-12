"""内置抖音弹幕采集器（原生签名直连，算法移植自 douyinLive v2.2.1，MIT）。

链路：房间号 → 首页自动取 ttwid（无需用户 cookie）→ web/enter 获取
room_id + 主播 pushID → im/fetch 预取 cursor/internal_ext/push_server →
16 位原生签名（hub.dysign.ws_sign）→ wss push/v2 直连 → 解 protobuf 帧 →
DyMessage dict → on_event 回调（与 Dycast 外挂输出同构，hub.events.normalize
直接消费）。

2026-09-03 实验验证：纯 Python 原生签名突破设备风控（旧 X-Bogus 链路被
DEVICE_BLOCKED 拦截）。config 里的 cookie 降级为可选增强（带上真实登录态
可提升信任度），不再是必需项。

保活：每 10s 发 hb 帧；协议层 ping/pong 判定死连接，应用层 300s 无任何帧
才判失联（冷清直播间几十秒无帧是常态，勿用短阈值误判）；服务端 needAck 回 ack；
payloadType=close 视为服务端主动断开。重连携带 cursor 续传，连续失败上限后
清空重整轮（重新 web/enter），断播/换场自愈。所有消息按帧层 msgId 进程级
LRU 去重（续传重发 / ACK 延迟重发 / 误起双连接都会重复投递）。
"""
import asyncio
import json
import re
import urllib.parse
from collections import OrderedDict

import httpx
import websockets

from hub.dysign import (DEFAULT_CURSOR, DEFAULT_PUSH_URL, default_internal_ext,
                        fetch_url_query, ms_token, normalize_push_url,
                        push_url_query, ws_sign, xms_stub)
from hub.dyproto import (decode_push_frame, decode_response,
                         encode_push_frame, response_dy_messages, _maybe_gunzip)
from hub.logger import get_logger

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/110.0.5481.77 Safari/537.36")
BROWSER_VERSION = UA[len("Mozilla/"):]
LIVE_HOME = "https://live.douyin.com/"
ENTER_API = "https://live.douyin.com/webcast/room/web/enter/"
FETCH_API = "https://live.douyin.com/webcast/im/fetch/"

HEARTBEAT_S = 10          # hb 发送间隔（与浏览器端一致）
RECV_TIMEOUT_S = 300      # 应用层兜底：300s 无任何帧 → 判定失联（协议层 ping/pong
                          # 才是死连接主判据；冷清房间几十秒无帧是常态，短阈值会误杀）
RECONNECT_DELAY_S = 5.0   # 单次重连间隔
NOT_LIVING_POLL_S = 30.0  # 主播未开播时的轮询间隔

# 消息去重（进程级 LRU，跨采集器实例共享）：cursor 续传重发、ACK 延迟触发的
# 服务端重发、误起双连接，都会把同一条消息重复送达。无 msg_id 的消息放行不猜。
_SEEN_MSG_IDS: "OrderedDict[str, None]" = OrderedDict()
_SEEN_MSG_IDS_CAP = 1000


def _seen_before(msg_id: str) -> bool:
    """msg_id 已见过返回 True（重复）；首次见登记后返回 False。"""
    if not msg_id:
        return False
    if msg_id in _SEEN_MSG_IDS:
        _SEEN_MSG_IDS.move_to_end(msg_id)
        return True
    _SEEN_MSG_IDS[msg_id] = None
    if len(_SEEN_MSG_IDS) > _SEEN_MSG_IDS_CAP:
        _SEEN_MSG_IDS.popitem(last=False)
    return False

ROOM_STATUS_TEXT = {
    1: "主播正在准备中", 2: "主播正在直播中", 3: "主播暂时离开了", 4: "主播已下播",
}


class DouyinError(Exception):
    """采集器可预期的失败（网络/解析/连接拒绝）。"""


def extract_room_num(raw: str) -> str:
    """兼容粘贴整条直播页 URL：取路径里的房间号，纯数字原样返回。"""
    text = str(raw or "").strip()
    if "live.douyin.com" in text:
        m = re.search(r"live\.douyin\.com/(\d+)", text)
        if m:
            return m.group(1)
    return text


def parse_enter_response(body: str) -> dict:
    """web/enter JSON → {room_id, push_id, status, title, nickname, prompts}。

    data.data[0] 为房间主体（id_str/title/status/owner），data.user 为主播
    （id_str/nickname）；任一缺失时对应字段为空串。
    prompts：主播设可见范围/房间不存在时抖音返回的可读原因。
    """
    try:
        raw = json.loads(body)
        payload = raw.get("data") or {}
    except (ValueError, AttributeError):
        return {"room_id": "", "push_id": "", "status": 0, "title": "", "nickname": "",
                "prompts": ""}
    entries = payload.get("data") or []
    room = entries[0] if entries else (payload.get("room") or {})
    user = payload.get("user") or (room.get("owner") or {})
    status_raw = room.get("status")
    prompts = str(payload.get("prompts") or "")
    return {
        "room_id": str(room.get("id_str") or room.get("id") or ""),
        "push_id": str(user.get("id_str") or user.get("id")
                       or room.get("owner_user_id_str") or ""),
        "status": int(status_raw) if isinstance(status_raw, int) else 0,
        "title": str(room.get("title") or ""),
        "nickname": str(user.get("nickname") or ""),
        "prompts": prompts,
    }


def _clean_cookie(raw: str) -> str:
    """清理/规范化粘贴的 cookie，兼容两种形态。

    - F12 复制的 Cookie 头串（单行 k=v; k=v）→ 仅去换行/首尾空白
    - Netscape cookies.txt 插件导出（Tab 分隔 7 字段，多行）→ 转 k=v; 串
      （#HttpOnly_ 前缀行去掉前缀照常解析；注释行与空名/空值行跳过）
    """
    text = str(raw or "").replace("\r", "")
    if "\t" not in text:
        return text.replace("\n", "").strip()
    pairs = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        name, value = fields[5].strip(), fields[6].strip()
        if name and value:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs) if pairs else text.strip()


class DouyinCollector:
    """内置弹幕采集器。run(stop) 阻塞运行直到 stop 置位；on_event 收 DyMessage dict。

    cookie：可选增强。非空时优先携带（真实浏览器登录态可提升风控信任度），
    默认空——采集链路只依赖首页自取的 ttwid，无需用户操作。
    """

    def __init__(self, room_num: str, on_event, max_reconnect: int = 3,
                 cookie: str = ""):
        room_num = extract_room_num(room_num)
        if not room_num:
            raise DouyinError("房间号为空：请在 config.yaml danmaku.room_num 填写抖音直播间房间号")
        self.room_num = room_num
        self._on_event = on_event
        self._max_reconnect = max_reconnect
        self.cookie = _clean_cookie(cookie)
        self._log = get_logger("douyin")
        self._log.info("collector_init", {
            "room_num": self.room_num, "external_cookie": bool(self.cookie),
        })

        self.room_id = ""
        self.push_id = ""         # 主播 user id（签名与 URL 的 user_unique_id）
        self.room_title = ""
        self.anchor = ""
        self.state = "idle"       # idle/connecting/live/reconnecting/wait_living/closed
        self.on_state_change = None  # (state: str) -> None：状态变化回调（main 装配 toast 用）
        self.reconnects = 0
        self.messages = 0
        self.last_error = ""
        self._ttwid = ""          # run() 从首页获取
        self._client_hint = {
            "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="110", "Google Chrome";v="110"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }

    # ── HTTP：房间信息与预取 ────────────────────────────────────────

    def _set_state(self, state: str) -> None:
        """状态切换入口：变化时触发 on_state_change 回调（main 用于连接状态 toast）。"""
        if state == self.state:
            return
        self.state = state
        cb = self.on_state_change
        if cb is not None:
            try:
                cb(state)
            except Exception:
                pass

    def _cookie_header(self) -> str:
        """请求携带的 Cookie 头：外部粘贴的完整登录态优先，缺 ttwid 时补首页取的。"""
        if self.cookie:
            if self._ttwid and "ttwid=" not in self.cookie:
                return f"{self.cookie}; ttwid={self._ttwid}"
            return self.cookie
        return f"ttwid={self._ttwid}" if self._ttwid else ""

    def _base_headers(self) -> dict:
        headers = {"User-Agent": UA, "Referer": LIVE_HOME,
                   "Origin": "https://live.douyin.com", **self._client_hint}
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _fetch_room_info(self, client: httpx.AsyncClient) -> None:
        """web/enter 接口获取 room_id / 主播 pushID / 直播状态。失败抛 DouyinError。"""
        query = "&".join([
            f"aid=6383", "app_name=douyin_web", "live_id=1",
            "device_platform=web", "language=zh-CN", "enter_from=link_share",
            "cookie_enabled=true", "screen_width=1920", "screen_height=1080",
            "browser_language=zh-CN", "browser_platform=Win32",
            "browser_name=Chrome",
            "browser_version=" + urllib.parse.quote(
                UA.split("Chrome/")[1].split(" ")[0], safe=""),
            "os_name=Windows", "os_version=10",
            "web_rid=" + urllib.parse.quote(self.room_num, safe=""),
            "room_id_str=", "enter_source=", "is_need_double_stream=false",
            "insert_task_id=", "live_reason=",
            "msToken=" + urllib.parse.quote(ms_token(), safe=""),
        ])
        resp = await client.get(ENTER_API + "?" + query,
                                headers={**self._base_headers(),
                                         "Accept": "application/json, text/plain, */*"})
        if resp.status_code != 200:
            raise DouyinError(f"web/enter HTTP {resp.status_code}")
        info = parse_enter_response(resp.text)
        if not info["room_id"] or not info["push_id"]:
            reason = info.get("prompts") or "检查房间号是否正确"
            raise DouyinError(f"web/enter：{reason}")
        self.room_id = info["room_id"]
        self.push_id = info["push_id"]
        self.room_title = info["title"]
        self.anchor = info["nickname"]
        self.room_status = info["status"]
        self._log.info("room_info", {
            "room_num": self.room_num, "room_id": self.room_id,
            "push_id": self.push_id, "status": info["status"],
            "title": self.room_title, "anchor": self.anchor,
        })

    async def _fetch_im_info(self, client: httpx.AsyncClient) -> dict:
        """im/fetch 预取 cursor / internal_ext / push_server + 初始消息。

        任一环节失败均回退默认值（DEFAULT_CURSOR / default_internal_ext /
        DEFAULT_PUSH_URL），链路不中断。
        """
        info = {
            "cursor": DEFAULT_CURSOR,
            "internal_ext": "",
            "push_url": DEFAULT_PUSH_URL,
        }
        query = fetch_url_query(self.room_id, self.push_id, BROWSER_VERSION,
                                ms_token())
        try:
            resp = await client.get(
                FETCH_API + "?" + query,
                headers={**self._base_headers(), "Accept": "*/*",
                         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
            if resp.status_code == 200 and len(resp.content) > 8:
                response = decode_response(resp.content)
                info["cursor"] = response.get("cursor") or info["cursor"]
                info["internal_ext"] = response.get("internal_ext") or ""
                for key in ("push_server_v2", "push_server", "proxy_server"):
                    push_url = normalize_push_url(response.get(key) or "")
                    if push_url:
                        info["push_url"] = push_url
                        break
                for dy in response_dy_messages(response):
                    self._dispatch(dy)
        except Exception as e:
            self._log.warning("im_fetch_failed", {"error": str(e)})
        if not info["internal_ext"]:
            info["internal_ext"] = default_internal_ext(self.room_id, self.push_id)
        return info

    def _dispatch(self, dy: dict) -> None:
        if _seen_before(str(dy.get("msg_id") or "")):
            self._log.debug("dup_msg_dropped", {"msg_id": dy.get("msg_id")})
            return
        try:
            self._on_event(dy)
        except Exception:
            self._log.exception("on_event_failed")

    # ── WS 会话 ─────────────────────────────────────────────────────

    async def _run_session(self, client: httpx.AsyncClient, im_info: dict,
                           stop: asyncio.Event) -> None:
        """单次 WS 会话：签名→连接→收帧→分发；失联/断开抛异常由外层决定重连。"""
        signature = ws_sign(xms_stub(self.room_id, self.push_id))
        url = (im_info["push_url"] + "?"
               + push_url_query(self.room_id, self.push_id, im_info["cursor"],
                                im_info["internal_ext"], signature,
                                BROWSER_VERSION))
        headers = {"User-Agent": UA, "Origin": "https://live.douyin.com",
                   "Referer": f"{LIVE_HOME}{self.room_num}"}
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie

        self._set_state("connecting")
        async with websockets.connect(url, additional_headers=headers,
                                       compression=None,
                                       max_size=16 * 1024 * 1024) as ws:
            self._set_state("live")
            self.reconnects = 0
            self._log.info("ws_open", {"room_id": self.room_id,
                                        "push_url": im_info["push_url"]})
            hb_task = asyncio.create_task(self._heartbeat_loop(ws, stop))
            try:
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT_S)
                    if isinstance(raw, str):
                        continue
                    im_info = await self._handle_frame(ws, raw, im_info) or im_info
            finally:
                hb_task.cancel()
                try:
                    await hb_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _handle_frame(self, ws, raw: bytes, im_info: dict) -> dict | None:
        """处理一帧：needAck 回执、close 退出、消息分发；返回更新后的续传信息。"""
        self.messages += 1
        frame = decode_push_frame(raw)
        payload = frame.get("payload")
        if payload is None:
            return None
        payload = _maybe_gunzip(payload, frame["headers"])
        response = decode_response(payload)

        if response["need_ack"]:
            ack = encode_push_frame(
                payload_type="ack",
                payload=(response.get("internal_ext") or "").encode("utf-8"),
                log_id=frame.get("log_id", 0),
            )
            await ws.send(ack)

        new_info = dict(im_info)
        if response.get("cursor"):
            new_info["cursor"] = response["cursor"]
        if response.get("internal_ext"):
            new_info["internal_ext"] = response["internal_ext"]
        for key in ("push_server_v2", "push_server", "proxy_server"):
            push_url = normalize_push_url(response.get(key) or "")
            if push_url:
                new_info["push_url"] = push_url
                break

        if frame.get("payload_type") == "close":
            self._log.info("server_close", {"room_id": self.room_id})
            raise websockets.ConnectionClosed(None, None)

        for dy in response_dy_messages(response):
            self._dispatch(dy)
        return new_info

    async def _heartbeat_loop(self, ws, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.sleep(HEARTBEAT_S)
            try:
                await ws.send(encode_push_frame(payload_type="hb"))
            except Exception as e:
                self._log.warning("hb_failed", {"error": str(e)})
                return

    # ── 主循环 ─────────────────────────────────────────────────────

    async def run(self, stop: asyncio.Event) -> None:
        """运行直到 stop 置位。未开播时轮询等待，断连自动恢复。"""
        timeout = httpx.Timeout(15.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            if self.cookie:
                self._log.info("external_cookie_used", {"length": len(self.cookie)})
            if not self.cookie or "ttwid=" not in self.cookie:
                self._ttwid = await self._ensure_ttwid(client)
            while not stop.is_set():
                try:
                    await self._collect_round(client, stop)
                except DouyinError as e:
                    self.last_error = str(e)
                    self._log.warning("round_error", {"error": str(e)})
                except Exception as e:  # 网络/WS 各类异常统一退避重试
                    self.last_error = f"{type(e).__name__}: {e}"
                    self._log.warning("round_error", {"error": self.last_error})
                if stop.is_set():
                    break
                try:
                    await asyncio.wait_for(stop.wait(), timeout=RECONNECT_DELAY_S)
                except asyncio.TimeoutError:
                    pass
        self._set_state("closed")
        self._log.info("collector_stopped", {"messages": self.messages})

    async def _collect_round(self, client: httpx.AsyncClient, stop: asyncio.Event) -> None:
        """一轮采集：web/enter → 未开播则等待 → im/fetch 预取 → 连接（含重连）直到断开。"""
        await self._fetch_room_info(client)
        status = getattr(self, "room_status", 0)
        if status and status != 2:
            self.last_error = ROOM_STATUS_TEXT.get(status, f"房间状态 {status}")
            self._set_state("wait_living")
            self._log.info("not_living", {"status": status})
            try:
                await asyncio.wait_for(stop.wait(), timeout=NOT_LIVING_POLL_S)
            except asyncio.TimeoutError:
                pass
            return

        im_info = await self._fetch_im_info(client)
        attempt = 0
        while not stop.is_set():
            attempt += 1
            try:
                await self._run_session(client, im_info, stop)
                return  # stop 置位后的正常退出
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                self._log.warning("session_error", {"attempt": attempt, "error": self.last_error})
                if stop.is_set():
                    return
                if attempt > self._max_reconnect:
                    self._log.warning("reconnect_exhausted", {"attempt": attempt})
                    return  # 回到外层：退避后重新 web/enter（跨场次自愈）
                self._set_state("reconnecting")
                self.reconnects += 1
                try:
                    await asyncio.wait_for(stop.wait(), timeout=RECONNECT_DELAY_S)
                except asyncio.TimeoutError:
                    pass

    async def _ensure_ttwid(self, client: httpx.AsyncClient) -> str:
        """访问直播首页获取 ttwid cookie（直连链路唯一的会话凭证）。"""
        try:
            resp = await client.get(LIVE_HOME, headers={"User-Agent": UA})
            ttwid = resp.cookies.get("ttwid", "")
            if ttwid:
                self._log.info("ttwid_ok")
                return ttwid
        except Exception as e:
            self._log.warning("ttwid_failed", {"error": str(e)})
        return ""

    def status(self) -> dict:
        """采集器状态快照（webui source_state 用）。"""
        return {
            "mode": "builtin",
            "room_num": self.room_num,
            "room_id": self.room_id,
            "title": self.room_title,
            "anchor": self.anchor,
            "state": self.state,
            "reconnects": self.reconnects,
            "messages": self.messages,
            "last_error": self.last_error,
            "cookie": bool(self.cookie),
        }
