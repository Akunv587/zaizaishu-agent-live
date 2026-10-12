"""可视化控制台服务（用户需求：把中枢做成可视化网页）。

同一端口双协议（websockets 17 的 process_request）：
- GET /          → 返回单文件控制台页 webui/index.html
- WS  /ws        → 实时数据流

推送协议（服务端→浏览器，均为 JSON）：
- snapshot：每秒一帧全量状态（统计/队列/在播/暂停/源连接/配置摘要）
- feed：连接建立时回放最近事件流（最多 100 条）
- event：事件即时增量（弹幕到达 / 播出开始 / 播出结束 / 系统通知）

回发协议（浏览器→服务端）：
- {"action": "toggle_pause"}        → 复用热键控制器（与 F9 同一代码路径）
- {"action": "test", "kind": "..."}  → 注入测试事件（与 Dycast 同一事件链路）
- {"action": "set_room", "room": "..."} → 热切换内置采集器房间号（main 装配 room_connector）
- {"action": "set_llm", "base_url": "...", "api_key": "...", "model": "...", "test": true}
  → 保存 LLM 配置并热生效（main 装配 llm_configurer；api_key 留空=保留现值；test 先验证连通）
- {"action": "vc_set_key", "api_key": "...", "endpoint": "...", "test": true}
  → 音色工坊保存 MiniMax Key（main 装配 tts_configurer；留空=保留现值；test 先验证连通）
- {"action": "vc_get"} → 返回 MiniMax 配置状态 + 音色库
- {"action": "vc_upload" / 二进制分片 / "vc_upload_end" / "vc_upload_abort"}
  → 上传待克隆音频（分片直传，单帧 ≤512KB，总量 ≤20MB）
- {"action": "vc_clone", "file_id": ..., "voice_id": "...", "voice_name": "..."}
  → 复刻新音色
- {"action": "vc_preview", "voice_id": "...", "text": "..."}
  → 克隆音色合成试听（返回音频直链，浏览器本地播放，不走直播声卡）
- {"action": "vw_fetch_voices"} → 一键拉取 MiniMax 账号下全部音色入库
- {"action": "vw_save_params", "voice_id": "...", "params": {...}}
  → 保存音色微调参数（语速/音量/音调/情绪），随音色档案持久化
- {"action": "vw_preview_params", "voice_id": "...", "text": "...", "params": {...}}
  → 用指定参数合成试听，跟随系统配置的输出设备播放（不走浏览器）
- {"action": "vw_delete_voice", "voice_id": "..."}
  → 删除账号音色：MiniMax 平台真实删除（不可恢复）+ 移出本地音色库；存档当前直播音色禁删

话术管理（v4 单方案多音色，main 装配 script_store / script_runtime / script_applier）：
- sc_list / sc_get / sc_save / sc_create / sc_rename / sc_delete / sc_set_active
  → 存档 CRUD 与激活切换（回复方案恒为纯AI，模板方案已退役）
- sc_voice_add {profile_id, voice_id, label} / sc_voice_remove / sc_voice_update
  → 存档音色管理（改备注/调参数）；sc_set_voice {profile_id, voice_id}
  → 切换存档当前直播音色（热生效，全链路发声统一走该音色）
- sc_upload（target=script_import）+ 二进制分片 + sc_upload_end → zip 存档导入
- sc_gen_audio {profile_id, kind=topic|sample, item_id, voice_id, emotion} → 单条×单音色×单情绪档
  合成音频（话题取 text / 样本取 reply_text），写 audios[voice_id][emotion]={file,text_hash}
- sc_gen_all {profile_id, voice_id?, emotion?} → 批量补齐缺失/过期音频（voice_id 缺省=全部音色，
  emotion 缺省=全部已登记档位；逐条广播 sc_gen_progress 进度，sc_gen_cancel {profile_id} 取消）
- sc_emotion_list {profile_id, voice_id?} → 情绪档位区数据（8 枚举可选 + 已登记档位 + 缺失统计）
- sc_emotion_save {profile_id, emotions: [...]} → 保存档位集合（default 恒在，非法档丢弃）
- sc_emotion_gen {profile_id, voice_id?, emotion} → 单档位批量补生成（只补该档缺失的条目）
- sc_emotion_purge {profile_id, voice_id?, emotion} → 清空该档音频（引用+文件）并移出档位集合
- sc_export {profile_id, path?} → 导出 zip（path 由桌面端「另存为」对话框给出；
  缺省写下载文件夹）；GET /sc_audio?pid=...&name=... → 音频直链
- cookie_get / cookie_set {cookie} → 采集 cookie 查询/保存（main 装配 cookie_configurer，
  保存后已连接房间自动重连生效）

智能体总开关（M3，main 装配 agent_status_provider / agent_switcher）：
- ag_status → 状态快照（enabled / available / llm_configured / mode / no_expire / settings）；
  快照帧的同名字段由 agent_status_provider 提供，前端卡片随快照刷新
- ag_set_enabled {enabled: bool} → 热切换总开关（写 config.yaml + 切 Router/Queue 模式，
  不重启）；未配 LLM Key 时拒绝开启并回明确原因（V3.3）

智能体页面（M5，main 装配 agent_logs_provider / agent_playground）：
- ag_logs {limit?} → 决策透视数据：{records: 最近 N 条决策（倒序在前端做）, summary: 四张
  聚合卡, path: 日志文件路径, available, note}；limit 夹在 1~AG_LOGS_MAX（默认 500）。
  未装配 provider 时回 available=false + 说明文案（页面显示"开启总开关后可见"）
- ag_playground {events: [{type,user,content,gift_name,gift_count,gift_value}, ...]} 或
  {batch: {type, count, ...}} → 试玩调试：逐条算完整决策（含 reason / recall / degrade /
  latency_ms），**不写记忆、不写决策日志、不进队列**（V5.3）。
  结果帧 type=`agent_playground`，含 {results, summary, ok, message}

反馈进化（M6，main 装配 feedback_store）：
- ag_feedback {kind: adopt|reject|rewrite, feedback: {...}, reasons?: [...], text?, note?}
  → 直播中枢那条 AI 回复旁的三个按钮。`feedback` 是后端随消息流下发、页面原样回传的
  上下文（event_type / user / content / gift_* / tone / reason / reply），字段口径只在
  后端定义一处。结果帧 type=`agent_feedback_done`：{ok, message, record, feedback_id,
  stats}（stats 顺带回给反馈进化区，省一次往返）
- ag_feedback_stats → 反馈进化区四项：{available, stats: {total, week_added, adopt_rate,
  reasons, ...}, note}；结果帧 type=`agent_feedback_stats`

对话调参（M6，main 装配 agent_session = hub.agent.session.SessionTuner）：
- ag_session_state → 调参区初始化：{available, reason, fields: 字段表, current: 当前值,
  snapshots: 最近快照（新的在前）}；结果帧 type=`agent_session_state`
- ag_session_send {text} → 一句自然语言指令（如「口播太频繁了」）。后端过 LLM function
  calling 落到白名单字段，写前留快照、写完热生效；每次最多改 MAX_TOOL_CALLS 项。
  结果帧 type=`agent_session_reply`：{kind:"send", ok, message, reply, changes:
  [{field, old, new, scope, note}], errors, snapshot_id}
- ag_session_undo {id?} → 撤销某次改动（不传 id 撤最近一次）；撤完该快照即被消费。
  结果帧 type=`agent_session_reply`：{kind:"undo", ok, message, restored}
  两条动作成功时都会顺带推一帧 `agent_session_state`（配置预览/快照列表免一次往返）

推送协议附加：
- vc：音色工坊操作结果（status / upload_done / clone_done / preview_done / error）
- script：话术管理操作结果（list / profile / saved / created / renamed / deleted /
  voice_added / voice_removed / voice_updated / voice_set / audio_generated /
  gen_progress / gen_done / import_done / exported / cookie / cookie_saved /
  emotions / emotions_saved / emotion_purged / error）

默认 enabled=false：仅 config.yaml 显式开启才监听（测试与小配置不打
端口战）；与 Dycast WS（8765）互不干扰，默认 127.0.0.1:8766。
"""
import asyncio
import json
import tempfile
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

from hub.config import (
    agent_settings,
    agent_status,
    cookie_configured,
    cookie_logged_in,
    llm_configured,
    mask_secret,
    minimax_configured,
)
from hub.logger import get_logger
from hub.paths import BUNDLE_DIR
from hub.scriptstore import (
    EMOTIONS_ALL,
    ScriptStoreError,
    audio_slot,
    normalize_emotion,
    normalize_emotions,
    text_hash,
)
from hub.voiceworkshop import VoiceWorkshop, WorkshopError, validate_voice_params

# 冻结后从 _MEIPASS 读内置页面；源码运行为项目根下 webui/index.html
PAGE_PATH = BUNDLE_DIR / "webui" / "index.html"
SNAPSHOT_INTERVAL_S = 1.0
FEED_MAX = 100
# 决策透视读取条数（M5）：默认 500（DESIGN §7.2④），上限 2000 防一帧塞进整天日志
AG_LOGS_DEFAULT = 500
AG_LOGS_MAX = 2000
VC_UPLOAD_CHUNK_MAX = 512 * 1024   # 单分片上限（前端切 256KB，留余量）
VC_UPLOAD_TOTAL_MAX = 20 * 1024 * 1024  # 与 MiniMax 音频上限一致
IMPORT_UPLOAD_TOTAL_MAX = 100 * 1024 * 1024  # zip 存档导入上限（含多个音频）
# 存档音频直链的 Content-Type（浏览器本地播放，不走直播声卡）
_AUDIO_MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}

# 可用输出设备缓存（60s 刷新：热插拔新设备后无需重启即可选）
_output_devices_cache: tuple[float, list] = (0.0, [])
_OUTPUT_DEVICES_TTL = 60.0


def _list_output_devices(refresh: bool = False) -> list[str]:
    """本机可用输出设备名列表（MME 条目优先；含 hostapi 后缀保证唯一性）。

    实测（步骤4.5）仅 MME 组合 24000Hz 可用：DirectSound 静音、WASAPI 报
    Invalid sample rate——所以下拉只列 MME（同一物理设备在 MME 下名称唯一），
    用户选到的即"能用"的条目。MME 全缺的极端环境回退列全部 API。
    """
    global _output_devices_cache
    now = time.time()
    if not refresh and now - _output_devices_cache[0] < _OUTPUT_DEVICES_TTL and _output_devices_cache[1]:
        return _output_devices_cache[1]
    result: list[str] = []
    try:
        import sounddevice as sd
        seen_mme: dict[str, None] = {}
        seen_all: dict[str, None] = {}
        for d in sd.query_devices():
            if d.get("max_output_channels", 0) <= 0:
                continue
            hostapi = sd.query_hostapis(d["hostapi"])["name"]
            entry = f"{d['name']}, {hostapi}"
            seen_all.setdefault(entry)
            if "MME" in hostapi.upper():
                seen_mme.setdefault(entry)
        result = list(seen_mme) or list(seen_all)
    except Exception:
        result = []
    _output_devices_cache = (now, result)
    return result


def resolve_output_device(name: str) -> int:
    """把设备名解析为 sounddevice 设备索引。

    匹配优先级：含 hostapi 后缀精确匹配 → 纯名称精确匹配 → 前缀匹配（兼容
    config.yaml 里的 MME 截断名如 "CABLE Input (VB-Audio Virtual C"）。
    裸名多命中时优先 MME：实测 DirectSound 静音、WASAPI 报采样率错，
    仅 MME 组合 24000Hz 可用（步骤4.5 联调结论）。
    """
    import sounddevice as sd
    devices = sd.query_devices()

    def _hostapi_name(d) -> str:
        return sd.query_hostapis(d["hostapi"])["name"]

    # 含 hostapi 后缀精确匹配（新下拉写入的格式，唯一）
    for i, d in enumerate(devices):
        if d.get("max_output_channels", 0) > 0:
            if f"{d['name']}, {_hostapi_name(d)}" == name:
                return i
    # 裸名匹配（旧 config）：三级判定收集全部命中，MME 全局优先（DirectSound
    # 静音、WASAPI 报采样率错——精确命中非 MME 也不如 MME 截断名命中），无
    # MME 候选再按精确度（精确 > 设备名是请求名前缀 > 请求名是设备名前缀）取第一个
    candidates: list[tuple[int, int]] = []
    for i, d in enumerate(devices):
        if d.get("max_output_channels", 0) <= 0:
            continue
        if d["name"] == name:
            candidates.append((0, i))
        elif name.startswith(d["name"]):
            candidates.append((1, i))
        elif d["name"].startswith(name):
            candidates.append((2, i))
    if candidates:
        candidates.sort()
        for _prio, i in candidates:
            if "MME" in _hostapi_name(devices[i]).upper():
                return i
        return candidates[0][1]
    raise ValueError(f"找不到输出设备「{name}」")


def _has_vb_cable(devices: list[str]) -> bool:
    """设备列表里是否包含 VB-Cable（含各种 API 前缀的 CABLE 条目）。"""
    return any("cable" in d.lower() for d in devices)


def _audio_devices_info(refresh: bool = False) -> dict:
    """设备列表 + VB-Cable 检测结果（快照/刷新接口共用）。"""
    devs = _list_output_devices(refresh=refresh)
    return {"list": devs, "has_vb_cable": _has_vb_cable(devs)}


class WebUi:
    """控制台服务。引用组件（player/hotkey 等）可装配后再注入，均为可选。"""

    def __init__(self, config: dict = None, stats=None, queue=None,
                 hotkey=None, player=None, source_state=None,
                 test_event_sender=None, room_connector=None, llm_configurer=None,
                 tts_configurer=None, voice_workshop=None,
                 log=None, clock=None):
        cfg = (config or {}).get("webui", {}) or {}
        self._enabled = bool(cfg.get("enabled", False))
        listen = str(cfg.get("listen", "127.0.0.1:8766") or "").strip()
        host, _, port = listen.rpartition(":")
        self._host = host or "127.0.0.1"
        self._port = int(port) if port else 8766
        self._config = config or {}
        self._stats = stats
        self._queue = queue
        self.hotkey = hotkey            # main 装配后注入
        self.player = player
        self.source_state = source_state          # () -> {"mode","connected",...}
        self.test_event_sender = test_event_sender  # (kind) -> None
        self.room_connector = room_connector      # (room_num: str) -> None 热切换房间
        self.room_disconnector = None             # () -> None 主动断开房间（main 装配注入）
        self.audio_device_changer = None          # (device: str) -> (ok, msg) 热切换输出声卡
        self.audio_device_tester = None           # async (device: str) -> (ok, msg) TTS 试听
        self.llm_configurer = llm_configurer      # async (base_url, api_key, model, test) 热配置 LLM
        self.tts_configurer = tts_configurer      # async (api_key, endpoint, voice_id, test) 热配置 TTS
        # v3.1 话术管理：main 装配注入（存档库 / 运行时 / 热应用回调）
        self.script_store = None                  # hub.scriptstore.ScriptStore
        self.script_runtime = None                # hub.selector.ScriptRuntime
        self.script_applier = None                # () -> None 存档变更后热生效
        self.cookie_configurer = None             # async (cookie: str) -> (ok, msg) 采集 cookie 热配置
        self.live_controller = None               # (live: bool) -> None 开始/结束直播（main 装配注入）
        self.live_getter = None                   # () -> bool 当前是否已开始直播（快照用）
        self.fallback_configurer = None           # async (text, enabled) -> (ok, msg) 兜底口播热配置
        # M3 智能体总开关：main 装配注入（状态快照 + 热切换）
        self.agent_status_provider = None         # () -> dict 智体状态（含 mode / no_expire）
        self.agent_switcher = None                # async (enabled: bool) -> (ok, msg) 热切换总开关
        # M5 智能体页面：main 装配注入（决策透视数据源 + 试玩调试执行器）
        self.agent_logs_provider = None           # (limit: int) -> {records, summary, path}
        self.agent_playground = None              # async ({events: [...]}) -> {results, summary}
        self.feedback_store = None                # M6：FeedbackStore（main 注入）
        self.agent_session = None                 # M6：SessionTuner 对话调参（main 注入）
        self.voice_workshop = voice_workshop or VoiceWorkshop()
        self._log = log or get_logger("webui")
        self._clock = clock or time.time
        self._server = None
        self._snap_task = None
        self._clients: set[asyncio.Queue] = set()
        self._feed: deque = deque(maxlen=FEED_MAX)
        self._gen_cancel: set[str] = set()  # 批量合成取消标记（profile_id）
        self._gen_running: set[str] = set()
        self._start_ts = self._clock()
        self._last_event_ts: float | None = None
        try:
            self._page = PAGE_PATH.read_text(encoding="utf-8")
        except OSError as e:
            self._enabled = False
            self._page = ""
            self._log.warning("webui_page_missing", {"path": str(PAGE_PATH), "error": str(e)})

    # ---------- 对外接口 ----------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self.port}/"

    @property
    def port(self) -> int:
        if self._server is not None and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return self._port

    async def start(self):
        self._server = await serve(self._handle_connection, self._host, self._port,
                                   process_request=self._process_http)
        self._snap_task = asyncio.create_task(self._snapshot_loop())
        self._log.info("webui_started", {"url": self.url})

    async def serve_forever(self):
        await self._server.serve_forever()

    async def close(self):
        if self._snap_task is not None:
            self._snap_task.cancel()
            try:
                await self._snap_task
            except asyncio.CancelledError:
                pass
            self._snap_task = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            self._log.info("webui_stopped")

    # ---------- 数据注入口（main 各环节调用，均为同步） ----------

    def note_event(self, ev) -> None:
        self._last_event_ts = ev.ts
        entry = {"kind": "danmaku", "ts": ev.ts, "type": ev.type, "user": ev.user,
                 "content": ev.content or "", "gift_name": ev.gift_name or "",
                 "gift_count": ev.gift_count or 0, "gift_value": ev.gift_value or 0}
        self._feed.append(entry)
        self._broadcast_event(entry)

    def note_play_start(self, task) -> None:
        entry = {"kind": "play_start", "ts": self._clock(), "source": task.source,
                 "priority": task.priority, "text": task.text, "tier": task.tier,
                 "merged_count": task.merged_count,
                 "test": bool(getattr(task, "test", False))}
        # M6 反馈进化：智能体决策的任务带 `feedback`（rules / 模板链路为 None）。
        # 前端据此在这条回复旁挂「采纳 / 弃用 / 改写」三个按钮；原样回传即入样本库。
        feedback = getattr(task, "feedback", None)
        if isinstance(feedback, dict) and feedback:
            entry["feedback"] = dict(feedback)
        self._feed.append(entry)
        self._broadcast_event(entry)

    def note_play_end(self, task) -> None:
        entry = {"kind": "play_end", "ts": self._clock(), "source": task.source,
                 "text": task.text}
        self._feed.append(entry)
        self._broadcast_event(entry)

    def note_system(self, text: str) -> None:
        entry = {"kind": "system", "ts": self._clock(), "text": text}
        self._feed.append(entry)
        self._broadcast_event(entry)

    def toast(self, text: str, level: str = "success") -> None:
        """顶部居中胶囊通知（前端渲染 success 绿 / error 红 / info 灰，自动消失可点关）。"""
        self._broadcast({"type": "toast",
                         "data": {"level": level, "text": str(text)}})

    # ---------- 快照 ----------

    def _build_snapshot(self) -> dict:
        now = self._clock()
        stats = self._stats.summary() if self._stats is not None else {}
        qstat = self._queue.qstat() if self._queue is not None else {}
        playing = bool(self.player.is_playing) if self.player is not None else False
        now_playing = self.player.current_info() if self.player is not None else None
        paused = bool(self.hotkey.paused) if self.hotkey is not None else False
        source = self.source_state() if self.source_state is not None else {}
        cfg = self._config
        llm = cfg.get("llm", {}) or {}
        tts = cfg.get("tts", {}) or {}
        audio = cfg.get("audio", {}) or {}
        return {
            "type": "snapshot", "ts": now,
            "uptime_s": round(now - self._start_ts, 1),
            "paused": paused, "playing": playing,
            "live": self.live_getter() if self.live_getter is not None else False,
            "now_playing": now_playing,
            "queue": qstat, "stats": stats,
            "last_event_age_s": (round(now - self._last_event_ts, 1)
                                 if self._last_event_ts is not None else None),
            "source": source,
            "config": {
                "llm_model": llm.get("model", ""),
                "llm_base_url": llm.get("base_url", ""),
                "llm_configured": llm_configured(cfg),
                "llm_key_masked": mask_secret(llm.get("api_key", "")),
                "voice_id": tts.get("voice_id", ""),
                "minimax_configured": minimax_configured(cfg),
                "minimax_key_masked": mask_secret(tts.get("api_key", "")),
                "device": audio.get("device", "") or "系统默认输出",
                "sample_rate": audio.get("sample_rate", 24000),
                "audio_devices": _audio_devices_info(),
                "hotkey": (cfg.get("safety", {}) or {}).get("pause_hotkey", "f9"),
                "fallback_reply": (cfg.get("safety", {}) or {}).get("fallback_reply", "哈哈这个咱们直播间聊哈"),
                "fallback_enabled": bool((cfg.get("safety", {}) or {}).get("fallback_enabled", True)),
                "cookie_configured": cookie_configured(cfg),
            },
            "script": (self.script_runtime.status()
                       if self.script_runtime is not None else {}),
            "agent": self._agent_status(),
        }

    async def _snapshot_loop(self):
        try:
            while True:
                await asyncio.sleep(SNAPSHOT_INTERVAL_S)
                if self._clients:
                    self._broadcast(self._build_snapshot())
        except asyncio.CancelledError:
            pass

    # ---------- 连接处理 ----------

    def _process_http(self, connection, request):
        """GET / 返回控制台页；/ws 交给 WS 升级；话术存档 zip 导出与音频直链；其余 404。"""
        parsed = urlparse(request.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return Response(200, "OK",
                            Headers([("Content-Type", "text/html; charset=utf-8")]),
                            self._page.encode("utf-8"))
        if path == "/ws":
            return None
        if path == "/sc_export":
            return self._http_sc_export(connection, parse_qs(parsed.query))
        if path == "/sc_audio":
            return self._http_sc_audio(connection, parse_qs(parsed.query))
        return connection.respond(404, "Not Found")

    def _http_sc_export(self, connection, query):
        """存档 zip 导出下载（GET /sc_export?pid=...）。"""
        pid = (query.get("pid") or [""])[0]
        if self.script_store is None or not pid:
            return connection.respond(404, "Not Found")
        try:
            with tempfile.NamedTemporaryFile(
                    suffix=".zip", prefix=f"zaizaishu-{pid}-", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                self.script_store.export_profile(pid, tmp_path)
                raw = Path(tmp_path).read_bytes()
            finally:
                Path(tmp_path).unlink(missing_ok=True)
        except ScriptStoreError as e:
            return connection.respond(404, str(e))
        except OSError as e:
            return connection.respond(500, f"导出失败：{e}")
        return Response(200, "OK", Headers([
            ("Content-Type", "application/zip"),
            ("Content-Disposition",
             f"attachment; filename=\"zaizaishu-profile-{pid}.zip\""),
        ]), raw)

    def _http_sc_audio(self, connection, query):
        """存档音频直链（GET /sc_audio?pid=...&name=...）：浏览器本地播放，不走直播声卡。"""
        pid = (query.get("pid") or [""])[0]
        name = (query.get("name") or [""])[0]
        if self.script_store is None or not pid or not name:
            return connection.respond(404, "Not Found")
        try:
            path = self.script_store.audio_path(pid, name)
        except ScriptStoreError:
            return connection.respond(404, "Not Found")
        if not path.is_file():
            return connection.respond(404, "Not Found")
        mime = _AUDIO_MIME.get(path.suffix.lower(), "application/octet-stream")
        try:
            raw = path.read_bytes()
        except OSError:
            return connection.respond(500, "音频读取失败")
        return Response(200, "OK", Headers([("Content-Type", mime)]), raw)

    async def _handle_connection(self, connection):
        outbox: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._clients.add(outbox)
        # 音色工坊分片上传缓冲（每连接独立；断开即随连接丢弃）
        vc_upload: dict = {"buf": None, "meta": None}
        # rx 退出（客户端断开）时投递 None 哨兵，唤醒下方 get()，
        # 否则 close() 取消快照循环后发送协程将永远阻塞，wait_closed 挂死。
        rx = asyncio.create_task(self._rx_loop(connection, outbox, vc_upload))
        try:
            outbox.put_nowait(self._dumps(self._build_snapshot()))
            outbox.put_nowait(self._dumps({"type": "feed", "data": list(self._feed)}))
            while True:
                msg = await outbox.get()
                if msg is None:
                    break
                await connection.send(msg)
        except ConnectionClosed:
            pass
        except Exception as e:
            self._log.debug("webui_client_error", {"error": f"{type(e).__name__}: {e}"})
        finally:
            rx.cancel()
            self._clients.discard(outbox)

    async def _rx_loop(self, connection, outbox, vc_upload):
        try:
            async for raw in connection:
                if isinstance(raw, bytes):
                    self._vc_chunk(raw, vc_upload)
                else:
                    await self._handle_action(raw, vc_upload)
        except ConnectionClosed:
            pass
        finally:
            try:
                outbox.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def _vc_chunk(self, raw: bytes, vc_upload: dict) -> None:
        """上传二进制分片（音色工坊/话术音频/存档导入共用）：追加到本连接缓冲。"""
        if vc_upload["buf"] is None:
            self._log.debug("webui_chunk_ignored", {"bytes": len(raw)})
            return
        vc_upload["buf"] += raw
        target = (vc_upload["meta"] or {}).get("target", "vc")
        limit = (IMPORT_UPLOAD_TOTAL_MAX if target == "script_import"
                 else VC_UPLOAD_TOTAL_MAX)
        if len(vc_upload["buf"]) > limit:
            vc_upload["buf"] = None
            vc_upload["meta"] = None
            self._broadcast_vc({"op": "error", "stage": "upload",
                                "error": f"文件超过 {limit // 1024 // 1024}MB 上限，已中止上传"})

    async def _handle_action(self, raw, vc_upload: dict = None) -> None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        action = str(data.get("action", "") or "")
        if action in ("toggle_pause", "pause", "resume") and self.hotkey is not None:
            hotkey = self.hotkey
            if action == "toggle_pause" or (action == "pause") != bool(hotkey.paused):
                asyncio.create_task(self._safe_toggle(hotkey))
        elif action == "test" and self.test_event_sender is not None:
            kind = str(data.get("kind", "chat"))
            payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
            label = {"chat": "弹幕", "gift": "礼物", "follow": "关注",
                     "enter": "进场", "fansclub": "粉丝团"}.get(kind, "事件")
            try:
                self.test_event_sender(kind, payload)
                self.toast(f"已注入测试{label}", "success")
            except Exception as e:
                self.note_system(f"测试事件注入失败: {type(e).__name__}: {e}")
                self.toast(f"测试{label}注入失败", "error")
                self._log.warning("webui_test_event_failed",
                                  {"error": f"{type(e).__name__}: {e}"})
        elif action == "set_live" and self.live_controller is not None:
            live = bool(data.get("live", False))
            self.live_controller(live)
            self.toast("已开始直播，话题轮播已启动" if live else "已结束直播，话题轮播已停止",
                       "success" if live else "info")
        elif action == "set_room" and self.room_connector is not None:
            room = str(data.get("room", "") or "").strip()
            if not room:
                self.note_system("连接房间失败：请先粘贴房间号或直播页链接")
                self.toast("请先粘贴房间号或直播页链接", "error")
                return
            try:
                self.room_connector(room)
                self.toast(f"正在连接房间 {room}", "info")
            except Exception as e:
                self.note_system(f"连接房间失败：{type(e).__name__}: {e}")
                self.toast(f"连接房间失败：{e}", "error")
                self._log.warning("webui_set_room_failed",
                                  {"error": f"{type(e).__name__}: {e}", "room": room})
        elif action == "disconnect_room" and self.room_disconnector is not None:
            self.room_disconnector()
        elif action == "set_audio_device" and self.audio_device_changer is not None:
            device = str(data.get("device", "") or "").strip()
            try:
                ok, msg = self.audio_device_changer(device)
                self.note_system(msg)
                self.toast("已切换输出声卡" if ok else f"切换失败：{msg}", "success" if ok else "error")
                self._broadcast({"type": "audio_device",
                                 "data": {"ok": bool(ok), "message": msg,
                                          "device": device}})
            except Exception as e:
                self._log.warning("webui_set_audio_device_failed",
                                  {"error": f"{type(e).__name__}: {e}", "device": device})
                self.note_system(f"切换输出设备失败：{type(e).__name__}: {e}")
                self.toast(f"切换输出设备失败：{e}", "error")
        elif action == "refresh_audio_devices":
            info = _audio_devices_info(refresh=True)
            self.toast(f"已刷新设备列表（{len(info['list'])} 个）", "success")
            self._broadcast({"type": "audio_device",
                             "data": {"op": "refresh",
                                      "devices": info["list"],
                                      "has_vb_cable": info["has_vb_cable"],
                                      "count": len(info["list"])}})
        elif action == "test_audio_device" and self.audio_device_tester is not None:
            device = str(data.get("device", "") or "").strip()
            asyncio.create_task(self._safe_test_audio(device))
        elif action == "set_llm" and self.llm_configurer is not None:
            asyncio.create_task(self._safe_set_llm(
                str(data.get("base_url", "") or ""),
                str(data.get("api_key", "") or ""),
                str(data.get("model", "") or ""),
                bool(data.get("test", False))))
        elif action == "set_fallback" and self.fallback_configurer is not None:
            asyncio.create_task(self._safe_set_fallback(
                str(data.get("text", "") or ""),
                bool(data.get("enabled", True))))
        elif action == "vc_set_key" and self.tts_configurer is not None:
            asyncio.create_task(self._safe_set_tts(
                str(data.get("api_key", "") or ""),
                str(data.get("endpoint", "") or ""),
                "", bool(data.get("test", False))))
        elif action == "vc_set_voice" and self.tts_configurer is not None:
            asyncio.create_task(self._safe_set_tts(
                "", "", str(data.get("voice_id", "") or ""), False))
        elif action == "vc_get":
            self._vc_reply_status()
        elif action == "vc_upload":
            self._vc_upload_start(data, vc_upload if vc_upload is not None else {})
        elif action == "vc_upload_end":
            asyncio.create_task(self._vc_upload_end(vc_upload if vc_upload is not None else {}))
        elif action == "vc_upload_abort":
            if vc_upload is not None:
                vc_upload["buf"] = None
                vc_upload["meta"] = None
        elif action == "vc_clone":
            asyncio.create_task(self._vc_clone(data))
        elif action == "vc_preview":
            asyncio.create_task(self._vc_preview(data))
        # ---------- 音色工坊扩展（参数微调 + 账号音色拉取） ----------
        elif action == "vw_fetch_voices":
            asyncio.create_task(self._vw_fetch_voices())
        elif action == "vw_save_params":
            self._vw_save_params(data)
        elif action == "vw_preview_params":
            asyncio.create_task(self._vw_preview_params(data))
        elif action == "vw_delete_voice":
            asyncio.create_task(self._vw_delete_voice(data))
        # ---------- 话术管理（v3.1） ----------
        elif action == "sc_list":
            self._sc_push_list()
        elif action == "sc_get":
            self._sc_get(str(data.get("profile_id", "") or ""))
        elif action == "sc_save":
            asyncio.create_task(self._sc_save(
                str(data.get("profile_id", "") or ""), data.get("profile") or {}))
        elif action == "sc_create":
            self._sc_create(str(data.get("name", "") or ""),
                            str(data.get("source_id", "") or "") or None)
        elif action == "sc_rename":
            self._sc_rename(str(data.get("profile_id", "") or ""),
                            str(data.get("name", "") or ""))
        elif action == "sc_delete":
            self._sc_delete(str(data.get("profile_id", "") or ""))
        elif action == "sc_set_active":
            self._sc_set_active(str(data.get("profile_id", "") or ""))
        elif action == "sc_voice_add":
            self._sc_voice_add(str(data.get("profile_id", "") or ""),
                               str(data.get("voice_id", "") or ""),
                               str(data.get("label", "") or ""))
        elif action == "sc_voice_remove":
            self._sc_voice_remove(str(data.get("profile_id", "") or ""),
                                  str(data.get("voice_id", "") or ""))
        elif action == "sc_voice_update":
            self._sc_voice_update(data)
        elif action == "sc_set_voice":
            self._sc_set_voice(str(data.get("profile_id", "") or ""),
                               str(data.get("voice_id", "") or ""))
        elif action == "sc_export":
            asyncio.create_task(self._sc_export(
                str(data.get("profile_id", "") or ""),
                str(data.get("path", "") or "")))
        elif action == "sc_upload":
            self._sc_upload_start(data, vc_upload if vc_upload is not None else {})
        elif action == "sc_upload_end":
            asyncio.create_task(self._sc_upload_end(vc_upload if vc_upload is not None else {}))
        elif action == "sc_audio_delete":
            self._sc_audio_delete(str(data.get("profile_id", "") or ""),
                                  str(data.get("name", "") or ""))
        elif action == "sc_gen_audio":
            asyncio.create_task(self._sc_gen_audio(
                str(data.get("profile_id", "") or ""),
                str(data.get("kind", "") or ""),
                str(data.get("item_id", "") or ""),
                str(data.get("voice_id", "") or ""),
                str(data.get("emotion", "") or "")))
        elif action == "sc_gen_all":
            asyncio.create_task(self._sc_gen_all(
                str(data.get("profile_id", "") or ""),
                str(data.get("voice_id", "") or ""),
                str(data.get("emotion", "") or "")))
        elif action == "sc_emotion_list":
            self._sc_emotion_list(str(data.get("profile_id", "") or ""),
                                  str(data.get("voice_id", "") or ""))
        elif action == "sc_emotion_save":
            asyncio.create_task(self._sc_emotion_save(
                str(data.get("profile_id", "") or ""), data.get("emotions") or []))
        elif action == "sc_emotion_gen":
            asyncio.create_task(self._sc_gen_all(
                str(data.get("profile_id", "") or ""),
                str(data.get("voice_id", "") or ""),
                str(data.get("emotion", "") or "")))
        elif action == "sc_emotion_purge":
            asyncio.create_task(self._sc_emotion_purge(
                str(data.get("profile_id", "") or ""),
                str(data.get("voice_id", "") or ""),
                str(data.get("emotion", "") or "")))
        elif action == "sc_gen_cancel":
            self._sc_gen_cancel(str(data.get("profile_id", "") or ""))
        elif action == "cookie_get":
            self._cookie_reply_status()
        elif action == "cookie_set" and self.cookie_configurer is not None:
            asyncio.create_task(self._safe_set_cookie(str(data.get("cookie", "") or "")))
        # ---------- 智能体总开关（M3） ----------
        elif action == "ag_status":
            self._broadcast({"type": "agent", "data": self._agent_status()})
        elif action == "ag_set_enabled":
            asyncio.create_task(self._ag_set_enabled(data.get("enabled")))
        # ---------- 智能体页面：决策透视 + 试玩调试（M5） ----------
        elif action == "ag_logs":
            self._ag_push_logs(data.get("limit"))
        elif action == "ag_playground":
            asyncio.create_task(self._ag_playground(data))
        # ---------- 反馈进化：采集 / 统计（M6） ----------
        elif action == "ag_feedback":
            asyncio.create_task(self._ag_feedback(data))
        elif action == "ag_feedback_stats":
            self._ag_push_feedback_stats()
        # ---------- 对话调参：状态 / 发指令 / 撤销（M6） ----------
        elif action == "ag_session_state":
            asyncio.create_task(self._ag_session_state())
        elif action == "ag_session_send":
            asyncio.create_task(self._ag_session_send(data))
        elif action == "ag_session_undo":
            asyncio.create_task(self._ag_session_undo(data))
        else:
            self._log.debug("webui_unknown_action", {"action": action})

    # ---------- 音色工坊（vc_* 动作处理；MiniMax 调用全走 to_thread 防阻塞） ----------

    def _broadcast_vc(self, data: dict) -> None:
        self._broadcast({"type": "vc", "data": data})

    def _vc_reply_status(self) -> None:
        ws = self.voice_workshop
        self._broadcast_vc({"op": "status",
                            "minimax": ws.minimax_status(),
                            "voices": ws.voices()})

    def _vc_upload_start(self, data: dict, vc_upload: dict) -> None:
        name = str(data.get("name", "") or "").strip() or "audio.wav"
        vc_upload["meta"] = {"target": "vc", "name": name}
        vc_upload["buf"] = bytearray()
        self._log.debug("webui_vc_upload_start", {"name": name})

    async def _vc_upload_end(self, vc_upload: dict) -> None:
        raw, meta = vc_upload["buf"], vc_upload["meta"]
        vc_upload["buf"], vc_upload["meta"] = None, None
        if raw is None or meta is None:
            self._broadcast_vc({"op": "error", "stage": "upload",
                                "error": "上传会话不存在（请重新选择音频文件）"})
            return
        try:
            rec = await asyncio.to_thread(
                self.voice_workshop.upload_audio, bytes(raw), meta["name"])
            self._broadcast_vc({"op": "upload_done", "record": rec})
            self.toast("音频上传完成", "success")
        except WorkshopError as e:
            self._broadcast_vc({"op": "error", "stage": "upload", "error": str(e)})
            self.toast(f"音频上传失败：{e}", "error")
        except Exception as e:
            self._broadcast_vc({"op": "error", "stage": "upload",
                                "error": f"{type(e).__name__}: {e}"})
            self.toast(f"音频上传失败：{e}", "error")

    async def _vc_clone(self, data: dict) -> None:
        self.toast("开始复刻音色…", "info")
        try:
            rec = await asyncio.to_thread(
                self.voice_workshop.clone_voice,
                data.get("file_id"),
                str(data.get("voice_id", "") or ""),
                str(data.get("model", "") or "") or None,
                str(data.get("voice_name", "") or ""))
            self._broadcast_vc({"op": "clone_done", "record": rec,
                                "voices": self.voice_workshop.voices()})
            self.toast("复刻完成", "success")
        except WorkshopError as e:
            self._broadcast_vc({"op": "error", "stage": "clone", "error": str(e)})
            self.toast(f"复刻失败：{e}", "error")
        except Exception as e:
            self._broadcast_vc({"op": "error", "stage": "clone",
                                "error": f"{type(e).__name__}: {e}"})
            self.toast(f"复刻失败：{e}", "error")

    async def _vc_preview(self, data: dict) -> None:
        self.toast("合成试听中…", "info")
        try:
            res = await asyncio.to_thread(
                self.voice_workshop.preview,
                str(data.get("text", "") or ""),
                str(data.get("voice_id", "") or ""),
                str(data.get("model", "") or "") or None)
            self._broadcast_vc({"op": "preview_done", **res})
            self.toast("试听合成完成", "success")
        except WorkshopError as e:
            self._broadcast_vc({"op": "error", "stage": "preview", "error": str(e)})
            self.toast(f"试听合成失败：{e}", "error")
        except Exception as e:
            self._broadcast_vc({"op": "error", "stage": "preview",
                                "error": f"{type(e).__name__}: {e}"})
            self.toast(f"试听合成失败：{e}", "error")

    # ---------- 音色工坊扩展（账号音色拉取 + 参数微调） ----------

    async def _vw_fetch_voices(self) -> None:
        """一键拉取 MiniMax 账号下全部音色（复刻/文生），合入音色库。"""
        self.toast("正在拉取账号音色…", "info")
        try:
            res = await asyncio.to_thread(self.voice_workshop.fetch_account_voices)
        except WorkshopError as e:
            self._broadcast_vc({"op": "error", "stage": "fetch_voices", "error": str(e)})
            self.toast(f"拉取失败：{e}", "error")
            return
        except Exception as e:
            self._broadcast_vc({"op": "error", "stage": "fetch_voices",
                                "error": f"{type(e).__name__}: {e}"})
            self.toast(f"拉取失败：{e}", "error")
            return
        added = len(res.get("added") or [])
        sys_n = res.get("system_count", 0)
        msg = (f"已拉取：新增 {added} 个音色（另有 {sys_n} 个系统音色未导入）"
               if added else f"无新增（账号音色已在库；系统音色 {sys_n} 个未导入）")
        self._broadcast_vc({"op": "fetch_voices_done", **res})
        self.note_system(msg)
        self.toast(msg, "success" if added else "info")

    def _vw_save_params(self, data: dict) -> None:
        """保存某音色的微调参数（语速/音量/音调/情绪），写音色库持久化。"""
        vid = str(data.get("voice_id", "") or "")
        try:
            rec = self.voice_workshop.set_voice_params(vid, data.get("params") or {})
        except WorkshopError as e:
            self._broadcast_vc({"op": "error", "stage": "save_params", "error": str(e)})
            self.toast(f"参数保存失败：{e}", "error")
            return
        except Exception as e:
            self._broadcast_vc({"op": "error", "stage": "save_params",
                                "error": f"{type(e).__name__}: {e}"})
            self.toast(f"参数保存失败：{e}", "error")
            return
        self._broadcast_vc({"op": "params_saved",
                            "voice_id": vid, "params": rec.get("params") or {}})
        self.note_system(f"音色参数已保存：{vid}")
        self.toast("音色参数已保存", "success")

    async def _vw_preview_params(self, data: dict) -> None:
        """参数微调试听：跟随系统配置的输出设备播放（main 装配 voice_param_previewer）。"""
        previewer = getattr(self, "voice_param_previewer", None)
        if previewer is None:
            self._broadcast_vc({"op": "error", "stage": "preview_params",
                                "error": "试听通道未就绪（voice_param_previewer 未装配）"})
            return
        vid = str(data.get("voice_id", "") or "")
        text = str(data.get("text", "") or "")
        params = data.get("params") if isinstance(data.get("params"), dict) else {}
        try:
            cleaned = validate_voice_params(params)
        except WorkshopError as e:
            self._broadcast_vc({"op": "error", "stage": "preview_params", "error": str(e)})
            self.toast(str(e), "error")
            return
        self.toast("合成试听中…", "info")
        try:
            ok, msg = await previewer(vid, text, cleaned)
        except Exception as e:
            ok, msg = False, f"{type(e).__name__}: {e}"
            self._log.warning("webui_preview_params_failed",
                              {"error": f"{type(e).__name__}: {e}", "voice_id": vid})
        self._broadcast_vc({"op": "preview_params_done", "ok": bool(ok),
                            "message": msg, "voice_id": vid})
        self.toast("试听完成" if ok else f"试听失败：{msg}",
                   "success" if ok else "error")

    async def _vw_delete_voice(self, data: dict) -> None:
        """删除账号音色：平台侧真实删除（不可恢复）+ 移出本地音色库。

        直播音色由工坊层拒绝（live_voice_id 从内存配置实时读取，
        与 vc_set_voice 热切换共享同一 cfg 字典）。
        """
        vid = str(data.get("voice_id", "") or "").strip()
        live_vid = str((self._config.get("tts", {}) or {}).get("voice_id", "") or "").strip()
        try:
            res = await asyncio.to_thread(
                self.voice_workshop.delete_voice, vid, live_vid)
        except WorkshopError as e:
            self._broadcast_vc({"op": "error", "stage": "delete_voice", "error": str(e)})
            self.toast(f"删除失败：{e}", "error")
            return
        except Exception as e:
            self._broadcast_vc({"op": "error", "stage": "delete_voice",
                                "error": f"{type(e).__name__}: {e}"})
            self.toast(f"删除失败：{e}", "error")
            return
        self._broadcast_vc({"op": "voice_deleted", **res})
        self.note_system(f"音色已从 MiniMax 平台删除：{vid}")
        self.toast(f"音色 {vid} 已删除（平台同步生效）", "success")

    # ---------- 话术管理（v3.1；sc_* 动作处理） ----------

    def _broadcast_sc(self, data: dict) -> None:
        self._broadcast({"type": "script", "data": data})

    def _sc_error(self, stage: str, error) -> None:
        self._broadcast_sc({"op": "error", "stage": stage, "error": str(error)})
        self.toast(f"操作失败：{error}", "error")

    def _sc_ready(self, stage: str) -> bool:
        if self.script_store is None:
            self._sc_error(stage, "话术管理未就绪（script_store 未装配）")
            return False
        self._ensure_synth()
        return True

    def _ensure_synth(self) -> None:
        """把存档音频合成通道接到音色工坊（幂等；首次用到 sc_* 时装配）。

        scriptstore 只做存储，合成实现由本层注入——这样 generate_audio 的
        「合成 → 落盘 → 写引用 → 清旧文件」全部收敛在存档层，便于单测注入假音色工坊。
        """
        if self.script_store is not None:
            self.script_store.set_synthesizer(self._synth_audio)

    def _synth_audio(self, text: str, voice_id: str, params: dict) -> bytes:
        """合成器适配：存档参数 → 音色工坊 synthesize_wav（返回 24kHz WAV 字节）。"""
        return self.voice_workshop.synthesize_wav(text, voice_id,
                                                 params=dict(params or {}))

    def _sc_applied(self) -> None:
        """激活存档内容 / 方案 / 指针变化后的热生效（reload + 应用，无需重启）。"""
        if self.script_runtime is not None:
            self.script_runtime.reload()
        if self.script_applier is not None:
            self.script_applier()

    def _sc_push_list(self) -> None:
        if not self._sc_ready("list"):
            return
        rt = self.script_runtime
        self._broadcast_sc({"op": "list",
                            "active": rt.profile_id if rt is not None else "",
                            "profiles": self.script_store.list_profiles()})

    def _sc_get(self, pid: str) -> None:
        if not self._sc_ready("profile"):
            return
        try:
            profile = self.script_store.get_profile(pid)
        except ScriptStoreError as e:
            self._sc_error("profile", e)
            return
        self._broadcast_sc({"op": "profile", "profile": profile})

    async def _sc_export(self, pid: str, path: str = "") -> None:
        """存档 zip 导出：path 由桌面端「另存为」对话框给出（浏览器模式缺省落下载文件夹）。

        WebView2 环境下载不落盘，桌面壳经 pywebview 原生对话框拿到目标路径后随
        action 回传；纯浏览器打开时前端走 GET /sc_export 直链下载。
        """
        if not self._sc_ready("export"):
            return
        if not pid or not self.script_store.exists(pid):
            self._sc_error("export", f"存档不存在：{pid}")
            return
        if path:
            dest = Path(path)
            if dest.suffix.lower() != ".zip":
                dest = dest.with_suffix(".zip")
        else:
            dest_dir = Path.home() / "Downloads"
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / f"zaizaishu-profile-{pid}.zip"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(self.script_store.export_profile, pid, str(dest))
        except (OSError, ScriptStoreError) as e:
            self._sc_error("export", e)
            return
        self._broadcast_sc({"op": "exported", "pid": pid, "path": str(dest)})
        self.toast("存档已导出", "success")
        self.note_system(f"存档已导出：{dest}")

    async def _sc_save(self, pid: str, data: dict) -> None:
        if not self._sc_ready("save"):
            return
        try:
            saved = await asyncio.to_thread(self.script_store.save_profile, pid, data)
        except ScriptStoreError as e:
            self._sc_error("save", e)
            return
        rt = self.script_runtime
        if rt is not None and rt.profile_id == pid:
            self._sc_applied()  # 改的是激活存档：立即热生效
        self._broadcast_sc({"op": "saved", "profile": saved})
        self.toast("话术已保存", "success")

    def _sc_create(self, name: str, source_id: str = None) -> None:
        if not self._sc_ready("create"):
            return
        try:
            profile = self.script_store.create_profile(name, source_id=source_id)
        except ScriptStoreError as e:
            self._sc_error("create", e)
            return
        self._broadcast_sc({"op": "created", "profile": profile})
        self.toast(f"已新建存档「{profile['name']}」", "success")
        self._sc_push_list()

    def _sc_rename(self, pid: str, name: str) -> None:
        if not self._sc_ready("rename"):
            return
        try:
            profile = self.script_store.rename_profile(pid, name)
        except ScriptStoreError as e:
            self._sc_error("rename", e)
            return
        self._broadcast_sc({"op": "renamed", "id": pid, "name": profile["name"]})
        self.toast("已重命名存档", "success")
        self._sc_push_list()

    def _sc_delete(self, pid: str) -> None:
        if not self._sc_ready("delete"):
            return
        try:
            self.script_store.delete_profile(pid)
        except ScriptStoreError as e:
            self._sc_error("delete", e)
            return
        rt = self.script_runtime
        if rt is not None and rt.profile_id == pid:
            # 删掉的是激活存档：兜底选定一个可用存档并写回指针（链路不断）
            from hub.config import update_script_config
            new_id = self.script_store.ensure_default()
            try:
                update_script_config(str(self._config.get("_config_path") or "config.yaml"),
                                     active_profile=new_id)
                self._config.setdefault("script", {})["active_profile"] = new_id
            except OSError as e:
                self._log.warning("script_pointer_write_failed", {"error": str(e)})
            self._sc_applied()
            self.note_system("激活存档已删除，已自动切换到其他存档")
        self._broadcast_sc({"op": "deleted", "id": pid})
        self.toast("已删除存档", "success")
        self._sc_push_list()

    def _sc_set_active(self, pid: str) -> None:
        if not self._sc_ready("set_active"):
            return
        if not self.script_store.exists(pid):
            self._sc_error("set_active", f"存档不存在：{pid}")
            return
        from hub.config import update_script_config
        try:
            update_script_config(str(self._config.get("_config_path") or "config.yaml"),
                                 active_profile=pid)
        except OSError as e:
            self._sc_error("set_active", f"指针写回失败：{e}")
            return
        self._config.setdefault("script", {})["active_profile"] = pid
        self._sc_applied()
        name = ""
        if self.script_runtime is not None and self.script_runtime.profile is not None:
            name = self.script_runtime.profile.get("name", "")
        self.note_system(f"已切换激活存档：{name or pid}")
        self.toast(f"已切换激活存档：{name or pid}", "success")
        self._sc_push_list()

    # ---------- 存档音色管理（v4：全链路发声统一走存档当前音色） ----------

    def _sc_voice_add(self, pid: str, voice_id: str, label: str) -> None:
        """把音色工坊库存音色挂入存档（首个音色自动成为当前直播音色）。"""
        if not self._sc_ready("voice_add"):
            return
        if not self.script_store.exists(pid):
            self._sc_error("voice_add", f"存档不存在：{pid}")
            return
        known = {str(v.get("voice_id") or ""): str(v.get("voice_name") or "")
                 for v in self.voice_workshop.voices()}
        if voice_id not in known:
            self._sc_error("voice_add",
                           f"音色 {voice_id or '（空）'} 不在音色工坊库中，请先在音色工坊克隆或拉取")
            return
        try:
            profile = self.script_store.add_voice(pid, voice_id, label or known[voice_id])
        except ScriptStoreError as e:
            self._sc_error("voice_add", e)
            return
        rt = self.script_runtime
        if rt is not None and rt.profile_id == pid:
            self._sc_applied()
        self._broadcast_sc({"op": "voice_added", "profile_id": pid,
                            "voice_id": voice_id, "profile": profile})
        self.toast(f"音色 {profile['voices'][-1]['label']} 已加入存档", "success")

    def _sc_voice_remove(self, pid: str, voice_id: str) -> None:
        """从存档移除音色（连带删除其音频；当前直播音色由 store 拒删）。"""
        if not self._sc_ready("voice_remove"):
            return
        try:
            profile = self.script_store.remove_voice(pid, voice_id)
        except ScriptStoreError as e:
            self._sc_error("voice_remove", e)
            return
        rt = self.script_runtime
        if rt is not None and rt.profile_id == pid:
            self._sc_applied()
        self._broadcast_sc({"op": "voice_removed", "profile_id": pid,
                            "voice_id": voice_id, "profile": profile})
        self.toast("音色已移出存档（其音频一并清除）", "success")

    def _sc_voice_update(self, data: dict) -> None:
        """改存档音色备注 / 微调参数（参数热生效：当前音色被改时立即重建 TTS）。"""
        pid = str(data.get("profile_id", "") or "")
        voice_id = str(data.get("voice_id", "") or "")
        if not self._sc_ready("voice_update"):
            return
        label = data.get("label") if "label" in data else None
        params = data.get("params") if isinstance(data.get("params"), dict) else None
        if params is not None:
            try:
                params = validate_voice_params(params)
            except WorkshopError as e:
                self._sc_error("voice_update", e)
                return
        try:
            profile = self.script_store.update_voice(pid, voice_id,
                                                     label=label, params=params)
        except ScriptStoreError as e:
            self._sc_error("voice_update", e)
            return
        rt = self.script_runtime
        if rt is not None and rt.profile_id == pid:
            self._sc_applied()
        self._broadcast_sc({"op": "voice_updated", "profile_id": pid,
                            "voice_id": voice_id, "profile": profile})
        self.toast("音色设置已保存", "success")

    def _sc_set_voice(self, pid: str, voice_id: str) -> None:
        """切换存档当前直播音色（激活存档切换即热生效，全链路发声走该音色）。"""
        if not self._sc_ready("set_voice"):
            return
        try:
            profile = self.script_store.set_current_voice(pid, voice_id)
        except ScriptStoreError as e:
            self._sc_error("set_voice", e)
            return
        rt = self.script_runtime
        if rt is not None and rt.profile_id == pid:
            self._sc_applied()
        label = next((v["label"] for v in profile["voices"]
                      if v["voice_id"] == voice_id), voice_id)
        self._broadcast_sc({"op": "voice_set", "profile_id": pid,
                            "voice_id": voice_id, "profile": profile})
        self.note_system(f"直播音色已切换：{label}（立即生效）")
        self.toast(f"直播音色已切换：{label}", "success")
        self._sc_push_list()

    def _sc_upload_start(self, data: dict, vc_upload: dict) -> None:
        """存档音频 / zip 导入上传开始（与音色工坊共用分片缓冲）。"""
        target = str(data.get("target", "") or "")
        if target not in ("script_audio", "script_import"):
            self._sc_error("upload", f"未知上传目标：{target!r}")
            return
        name = str(data.get("name", "") or "").strip() or (
            "audio.wav" if target == "script_audio" else "profile.zip")
        vc_upload["meta"] = {"target": target, "name": name,
                             "profile_id": str(data.get("profile_id", "") or ""),
                             "kind": str(data.get("kind", "") or ""),
                             "item_id": str(data.get("item_id", "") or ""),
                             "voice_id": str(data.get("voice_id", "") or ""),
                             "emotion": str(data.get("emotion", "") or "")}
        vc_upload["buf"] = bytearray()
        self._log.debug("webui_sc_upload_start", {"target": target, "name": name})

    async def _sc_upload_end(self, vc_upload: dict) -> None:
        raw, meta = vc_upload["buf"], vc_upload["meta"]
        vc_upload["buf"], vc_upload["meta"] = None, None
        if raw is None or meta is None:
            self._sc_error("upload", "上传会话不存在（请重新选择文件）")
            return
        if not self._sc_ready("upload"):
            return
        target, name = meta.get("target"), meta.get("name", "")
        pid = meta.get("profile_id", "")
        try:
            if target == "script_audio":
                # v4 多音色 × 多情绪：上传即归属「音色 + 情绪档」，直接写 audios[voice][emotion]
                kind, item_id = meta.get("kind", ""), meta.get("item_id", "")
                voice_id = str(meta.get("voice_id", "") or "")
                emotion = normalize_emotion(meta.get("emotion", ""))
                profile, item, text_key = self._sc_find_item(pid, kind, item_id)
                if item is None:
                    return
                text = str(item.get(text_key) or "").strip()
                if not voice_id or not any(
                        v.get("voice_id") == voice_id
                        for v in profile.get("voices") or []):
                    self._sc_error("upload", f"音色 {voice_id or '（空）'} 不在存档音色列表中")
                    return
                filename = await asyncio.to_thread(
                    self.script_store.save_audio, pid, bytes(raw), name)

                def _attach() -> None:
                    old = self.script_store.set_audio_ref(item, voice_id, emotion,
                                                          filename, text)
                    if emotion not in list(profile.get("emotions") or []):
                        profile["emotions"] = normalize_emotions(
                            list(profile.get("emotions") or []) + [emotion])
                    self.script_store.save_profile(pid, profile)
                    if old and old != filename:
                        self.script_store.delete_audio(pid, old)

                await asyncio.to_thread(_attach)
                rt = self.script_runtime
                if rt is not None and rt.profile_id == pid:
                    self._sc_applied()
                self._broadcast_sc({"op": "audio_generated", "profile_id": pid,
                                    "kind": kind, "item_id": item_id,
                                    "audio": filename, "voice_id": voice_id,
                                    "emotion": emotion, "text_hash": text_hash(text)})
                self.toast("音频已上传", "success")
            else:
                src = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
                try:
                    src.write(bytes(raw))
                    src.close()
                    profile, warnings = await asyncio.to_thread(
                        self.script_store.import_profile, src.name)
                finally:
                    Path(src.name).unlink(missing_ok=True)
                self._broadcast_sc({"op": "import_done", "profile": profile,
                                    "warnings": warnings})
                self._sc_push_list()
                self.note_system(f"存档「{profile['name']}」导入完成"
                                 + (f"（{len(warnings)} 条警告）" if warnings else ""))
                self.toast(f"存档「{profile['name']}」导入完成", "success")
        except ScriptStoreError as e:
            self._sc_error("upload", e)
        except OSError as e:
            self._sc_error("upload", f"文件处理失败：{e}")

    def _sc_audio_delete(self, pid: str, name: str) -> None:
        if not self._sc_ready("audio_delete"):
            return
        try:
            self.script_store.delete_audio(pid, name)
        except ScriptStoreError as e:
            self._sc_error("audio_delete", e)
            return
        self._broadcast_sc({"op": "audio_deleted", "profile_id": pid, "audio": name})
        self.toast("音频已清除", "success")

    # 单条合成与批量补齐共用的条目定位表
    _SC_KIND = {"topic": ("topics_ai", "text"), "sample": ("samples_ai", "reply_text")}

    def _sc_find_item(self, pid: str, kind: str, item_id: str):
        """定位条目：返回 (profile, item, text_key)；失败广播错误并返回 (None, None, None)。"""
        pair = self._SC_KIND.get(kind)
        if pair is None:
            self._sc_error("gen_audio", f"未知条目类型：{kind!r}（可选 topic / sample）")
            return None, None, None
        section_key, text_key = pair
        try:
            profile = self.script_store.get_profile(pid)
        except ScriptStoreError as e:
            self._sc_error("gen_audio", e)
            return None, None, None
        item = next((x for x in profile.get(section_key) or []
                     if x.get("id") == item_id), None)
        if item is None:
            self._sc_error("gen_audio", "条目不存在（可能已被删除，请刷新列表）")
            return None, None, None
        return profile, item, text_key

    async def _sc_gen_audio(self, pid: str, kind: str, item_id: str, voice_id: str,
                            emotion: str = "") -> None:
        """单条 × 单音色 × 单情绪档音频生成。

        kind=topic 取 topics_ai 的 text；kind=sample 取 samples_ai 的 reply_text。
        voice_id 缺省=存档当前音色；emotion 缺省=default 档。
        合成 → 落盘 → 写 audios[voice][emotion] → 清旧文件的全部动作在
        ScriptStore.generate_audio 里（合成通道由 _ensure_synth 注入）；
        条目按 id 跨分区定位，此处先校验 kind 以拦住前端协议笔误。
        """
        if not self._sc_ready("gen_audio"):
            return
        if kind not in self._SC_KIND:
            self._sc_error("gen_audio", f"未知条目类型：{kind!r}（可选 topic / sample）")
            return
        emo = normalize_emotion(emotion)
        try:
            res = await asyncio.to_thread(self.script_store.generate_audio,
                                          pid, item_id, voice_id, emo)
        except ScriptStoreError as e:
            self._sc_error("gen_audio", e)
            return
        rt = self.script_runtime
        if rt is not None and rt.profile_id == pid:
            self._sc_applied()
        self._broadcast_sc({"op": "audio_generated", "profile_id": pid,
                            "kind": res["kind"], "item_id": item_id,
                            "audio": res["audio"], "voice_id": res["voice_id"],
                            "emotion": res["emotion"],
                            "text_hash": res["text_hash"]})
        label = "话题" if res["kind"] == "topic" else "样本"
        self.note_system(f"{label}音频生成完成（音色 {res['voice_id']} · "
                         f"情绪 {res['emotion']}）")
        self.toast(f"{label}音频已生成", "success")

    def _sc_gen_cancel(self, pid: str) -> None:
        """取消进行中的批量合成（标记位，合成循环逐条检查）。"""
        if pid in self._gen_running:
            self._gen_cancel.add(pid)
            self.toast("正在取消批量生成…", "info")

    async def _sc_gen_all(self, pid: str, voice_id: str = "",
                          emotion: str = "") -> None:
        """批量补齐缺失/过期音频：voice_id 缺省=存档全部音色，逐条广播进度。

        待办组合 = 音色 × 情绪档（档位取自存档 emotions）；emotion 给定时只补该档
        （未登记的档位先自动登记，避免"生成了却用不上"）。跳过条件：条目停用、
        文本为空、该「音色 × 档位」已有引用且 text_hash 匹配且文件仍在；
        单条失败记 failed 继续，sc_gen_cancel 置位后中断剩余条目。
        """
        if not self._sc_ready("gen_all"):
            return
        if pid in self._gen_running:
            self._sc_error("gen_all", "该存档已有批量生成任务进行中")
            return
        try:
            profile = self.script_store.get_profile(pid)
        except ScriptStoreError as e:
            self._sc_error("gen_all", e)
            return
        voices = profile.get("voices") or []
        if voice_id:
            voices = [v for v in voices if v.get("voice_id") == voice_id]
            if not voices:
                self._sc_error("gen_all", f"音色 {voice_id} 不在存档音色列表中")
                return
        if not voices:
            self._sc_error("gen_all", "存档还没有音色——请先添加音色再批量生成")
            return
        emotions = normalize_emotions(profile.get("emotions"))
        if emotion:
            emo = normalize_emotion(emotion)
            emotions = [emo]
            if emo not in list(profile.get("emotions") or []):
                # 指定档位尚未登记：先登记再生，保证生成的音频真的会被播放链路采用
                try:
                    profile = await asyncio.to_thread(
                        self.script_store.set_emotions, pid,
                        list(profile.get("emotions") or []) + [emo])
                except ScriptStoreError as e:
                    self._sc_error("gen_all", e)
                    return

        # 待办清单：(kind, item, text, voice, emotion) × 需补齐的组合
        todo = []
        for kind, (section_key, text_key) in self._SC_KIND.items():
            for item in profile.get(section_key) or []:
                if not item.get("enabled"):
                    continue
                text = str(item.get(text_key) or "").strip()
                if not text:
                    continue
                want = text_hash(text)
                for v in voices:
                    vid = v.get("voice_id")
                    for emo in emotions:
                        ref = audio_slot(item, vid, emo)
                        name = str(ref.get("file") or "")
                        if (name and str(ref.get("text_hash") or "") == want
                                and self.script_store.audio_exists(pid, name)):
                            continue
                        todo.append((kind, item.get("id"), text, v, emo))
        if not todo:
            self._broadcast_sc({"op": "gen_done", "profile_id": pid, "total": 0,
                                "done": 0, "failed": [], "cancelled": False})
            self.toast("所有音频均已就绪，无需生成", "info")
            return

        self._gen_running.add(pid)
        self._gen_cancel.discard(pid)
        total = len(todo)
        done, failed = 0, []
        try:
            for kind, item_id, text, voice, emo in todo:
                if pid in self._gen_cancel:
                    break
                vid = voice.get("voice_id")
                try:
                    await asyncio.to_thread(self.script_store.generate_audio,
                                            pid, item_id, vid, emo)
                    done += 1
                except ScriptStoreError as e:
                    failed.append({"item_id": item_id, "voice_id": vid,
                                   "kind": kind, "emotion": emo, "error": str(e)})
                    self._log.warning("sc_gen_all_item_failed",
                                      {"pid": pid, "item": item_id, "voice": vid,
                                       "emotion": emo, "error": str(e)})
                self._broadcast_sc({"op": "gen_progress", "profile_id": pid,
                                    "voice_id": vid, "emotion": emo, "total": total,
                                    "done": done, "failed": failed,
                                    "current": text[:20]})
        finally:
            self._gen_running.discard(pid)
            self._gen_cancel.discard(pid)
        cancelled = done + len(failed) < total
        rt = self.script_runtime
        if rt is not None and rt.profile_id == pid and (done or failed):
            self._sc_applied()
        self._broadcast_sc({"op": "gen_done", "profile_id": pid, "total": total,
                            "done": done, "failed": failed, "cancelled": cancelled,
                            "emotion": normalize_emotion(emotion) if emotion else ""})
        if cancelled:
            self.toast(f"批量生成已取消（完成 {done}/{total}）", "info")
        elif failed:
            self.toast(f"批量生成完成：成功 {done} 条，失败 {len(failed)} 条（可单独重试）",
                       "error")
        else:
            self.toast(f"批量生成完成：{done} 条音频全部就绪", "success")
        self.note_system(f"存档批量合成结束：{done}/{total} 成功"
                         + (f"，{len(failed)} 条失败" if failed else ""))

    # ---------- 情绪档位（v4；话术管理页「情绪档位」区） ----------

    def _sc_emotion_list(self, pid: str, voice_id: str = "") -> None:
        """情绪档位区数据：8 枚举可选清单 + 已登记档位 + 该音色逐档缺失统计。"""
        if not self._sc_ready("emotion_list"):
            return
        try:
            profile = self.script_store.get_profile(pid)
            report = self.script_store.missing_report(pid, voice_id)
        except ScriptStoreError as e:
            self._sc_error("emotion_list", e)
            return
        self._broadcast_sc({"op": "emotions", "profile_id": pid,
                            "available": list(EMOTIONS_ALL),
                            "emotions": list(profile.get("emotions") or []),
                            "voice_id": report["voice_id"],
                            "total": report["total"],
                            "missing": report["missing"]})

    async def _sc_emotion_save(self, pid: str, emotions) -> None:
        """保存情绪档位集合（default 恒在；非法档位静默丢弃）。"""
        if not self._sc_ready("emotion_save"):
            return
        try:
            saved = await asyncio.to_thread(self.script_store.set_emotions,
                                            pid, emotions)
        except ScriptStoreError as e:
            self._sc_error("emotion_save", e)
            return
        rt = self.script_runtime
        if rt is not None and rt.profile_id == pid:
            self._sc_applied()
        self._broadcast_sc({"op": "emotions_saved", "profile_id": pid,
                            "profile": saved,
                            "emotions": list(saved.get("emotions") or [])})
        self.note_system("情绪档位已更新：" + "、".join(saved.get("emotions") or []))
        self.toast("情绪档位已保存", "success")

    async def _sc_emotion_purge(self, pid: str, voice_id: str,
                                emotion: str) -> None:
        """清空某音色某情绪档的全部音频（引用 + 文件），并把该档移出档位集合。"""
        if not self._sc_ready("emotion_purge"):
            return
        try:
            res = await asyncio.to_thread(self.script_store.purge_emotion,
                                          pid, voice_id, normalize_emotion(emotion))
        except ScriptStoreError as e:
            self._sc_error("emotion_purge", e)
            return
        rt = self.script_runtime
        if rt is not None and rt.profile_id == pid:
            self._sc_applied()
        self._broadcast_sc({"op": "emotion_purged", "profile_id": pid,
                            "voice_id": res["voice_id"], "emotion": res["emotion"],
                            "files": res["files"],
                            "emotions": res["emotions"]})
        self.toast(f"已清空 {res['emotion']} 档音频（{res['files']} 个文件）", "info")
        self.note_system(f"情绪档位 {res['emotion']} 已清空并移出档位集合")

    # ---------- 采集 Cookie（系统配置页 Cookie 卡） ----------

    def _cookie_reply_status(self) -> None:
        cookie = str((self._config.get("danmaku", {}) or {}).get("cookie", "") or "")
        first_line = cookie.strip().split("\n", 1)[0] if cookie.strip() else ""
        self._broadcast_sc({"op": "cookie",
                            "configured": cookie_configured(self._config),
                            "logged_in": cookie_logged_in(self._config),
                            "chars": len(cookie.strip()),
                            "preview": mask_secret(first_line)})

    async def _safe_set_cookie(self, cookie: str) -> None:
        try:
            ok, message = await self.cookie_configurer(cookie)
        except Exception as e:
            ok, message = False, f"Cookie 保存失败：{type(e).__name__}: {e}"
            self._log.warning("webui_set_cookie_failed",
                              {"error": f"{type(e).__name__}: {e}"})
        self.note_system(message)
        if not cookie.strip():
            self.toast("Cookie 已清空", "success")
        else:
            self.toast("Cookie 已保存" if ok else f"Cookie 保存失败：{message}",
                       "success" if ok else "error")
        self._broadcast_sc({"op": "cookie_saved", "ok": bool(ok), "message": message,
                            "configured": cookie_configured(self._config),
                            "logged_in": cookie_logged_in(self._config)})

    # ---------- 智能体总开关（M3，系统配置页智能体卡） ----------

    def _agent_status(self) -> dict:
        """智能体状态快照：优先用 main 注入的 provider（含 router/queue 实时模式），
        未装配时退回纯配置口径（测试与降级路径都能拿到完整字段）。"""
        data = None
        if self.agent_status_provider is not None:
            try:
                got = self.agent_status_provider()
                data = dict(got) if isinstance(got, dict) else None
            except Exception as e:
                self._log.warning("webui_agent_status_failed",
                                  {"error": f"{type(e).__name__}: {e}"})
        if data is None:
            data = agent_status(self._config)
        # 字段兜底：前端卡片不因缺字段而渲染空值
        data.setdefault("enabled", False)
        data.setdefault("available", llm_configured(self._config))
        data.setdefault("llm_configured", llm_configured(self._config))
        data.setdefault("mode", "rules" if not data["enabled"] else "agent")
        data.setdefault("no_expire", bool(data["enabled"]))
        data.setdefault("settings", agent_settings(self._config))
        return data

    async def _ag_set_enabled(self, enabled) -> None:
        """总开关热切换（V3.2/V3.3）：main 的 agent_switcher 负责写配置 + 切模式。

        未装配 switcher（未注入）时明确回失败，不静默改本地 config——
        配置与运行时是两份状态，只改一份会让 UI 与实际行为不一致。
        """
        want = bool(enabled)
        if self.agent_switcher is None:
            self._ag_reply(False, "智能体开关不可用：运行时未装配切换器")
            return
        # V3.3：未配 LLM Key 时不允许开启（前端也会置灰，这里是服务端兜底）
        if want and not llm_configured(self._config):
            self._ag_reply(False, "未配置 LLM Key，无法开启智能体（请先在「LLM」卡配置）")
            return
        try:
            ok, message = await self.agent_switcher(want)
        except Exception as e:
            ok, message = False, f"切换失败：{type(e).__name__}: {e}"
            self._log.warning("webui_agent_switch_failed",
                              {"enabled": want, "error": f"{type(e).__name__}: {e}"})
        self._ag_reply(bool(ok), str(message or ""))

    def _ag_reply(self, ok: bool, message: str) -> None:
        """回推开关结果：toast + note_system + agent 状态（前端按钮复位 + 卡片留说明）。

        data 里带 op/ok/message 供卡片就地显示结果（快照帧没有 op，卡片据此不冲掉
        刚出的结果），顶层 ok 供按钮复位与协议侧判读。
        """
        if message:
            self.note_system(message)
        if ok:
            self.toast(message or "智能体开关已切换", "success")
        else:
            self.toast(message or "智能体开关切换失败", "error")
        data = self._agent_status()
        data["op"] = "ag_saved"
        data["ok"] = bool(ok)
        data["message"] = message
        self._broadcast({"type": "agent", "data": data, "ok": bool(ok)})

    # ---------- 智能体页面：决策透视 + 试玩调试（M5） ----------

    def _ag_push_logs(self, limit=None) -> None:
        """决策透视（DESIGN §7.2④）：最近 N 条决策 + 四张聚合卡。

        数据源由 main 注入的 `agent_logs_provider(limit)` 提供（它拿着 brain 的
        `DecisionLogger`）。这里刻意不 import `hub.agent.*`——webui 只负责"取数并广播"，
        日志从哪来（brain / 另一个进程 / 明天换成数据库）不归它管。

        limit 会被夹到 1~`AG_LOGS_MAX`：页面默认要 500 条，但别让一个手改的请求
        把整天的日志（弹幕风暴下一天上万条）一次性塞进 WS 帧。
        """
        lim = AG_LOGS_DEFAULT
        try:
            if limit is not None:
                lim = max(1, min(AG_LOGS_MAX, int(limit)))
        except (TypeError, ValueError):
            lim = AG_LOGS_DEFAULT
        if self.agent_logs_provider is None:
            self._broadcast_ag_logs({"available": False, "records": [], "summary": {},
                                     "path": "", "note": "智能体未装配：开启总开关后这里会显示决策记录"})
            return
        try:
            got = self.agent_logs_provider(lim)
            data = dict(got) if isinstance(got, dict) else {}
        except Exception as e:
            self._log.warning("webui_agent_logs_failed", {"error": f"{type(e).__name__}: {e}"})
            self._broadcast_ag_logs({"available": True, "records": [], "summary": {},
                                     "path": "", "note": f"读取决策日志失败：{e}"})
            return
        data.setdefault("available", True)
        data.setdefault("records", [])
        data.setdefault("summary", {})
        data.setdefault("path", "")
        data.setdefault("note", "")
        self._broadcast_ag_logs(data)

    def _broadcast_ag_logs(self, data: dict) -> None:
        self._broadcast({"type": "agent_logs", "data": data,
                         "ok": bool(data.get("available"))})

    async def _ag_playground(self, data: dict) -> None:
        """试玩调试（DESIGN §7.2②）：算决策但**不碰直播**（记忆/日志/队列都不动，V5.3）。

        两种投递入口：
        - `events: [...]` —— 页面表单逐条构造的事件（含批量投递造的批次）；
        - `batch: {type, count, ...}` —— 一键造同类型 N 条（模拟弹幕风暴）。

        失败（表单非法 / 未装配 / 上游异常）回 `ok: false` 的帧 + 就近提示；
        单条决策失败不在此处兜——`Playground` 已把它记在该条的 `error` 上，页面按条显示。
        """
        if self.agent_playground is None:
            self._ag_playground_reply(False, "试玩不可用：智能体未装配（请先开启总开关并配置 LLM Key）")
            return
        payload = self._playground_payload(data)
        if payload is None:
            self._ag_playground_reply(False, "试玩请求为空：请先构造至少一条事件")
            return
        try:
            result = await self.agent_playground(payload)
        except Exception as e:
            message = str(e) or f"{type(e).__name__}"
            self._log.warning("webui_agent_playground_failed",
                              {"error": f"{type(e).__name__}: {e}"})
            self._ag_playground_reply(False, message)
            return
        out = dict(result) if isinstance(result, dict) else {}
        out.setdefault("results", [])
        out.setdefault("summary", {})
        out["ok"] = True
        self._broadcast({"type": "agent_playground", "data": out, "ok": True})

    @staticmethod
    def _playground_payload(data: dict) -> dict | None:
        """页面请求 → `Playground.run` 的载荷；空请求返回 None。"""
        events = data.get("events")
        if isinstance(events, list) and events:
            return {"events": events}
        batch = data.get("batch")
        if isinstance(batch, dict) and batch.get("type"):
            return {"batch": batch}
        return None

    def _ag_playground_reply(self, ok: bool, message: str) -> None:
        if message:
            self.note_system(message)
        self.toast(message or "试玩失败", "success" if ok else "error")
        self._broadcast({"type": "agent_playground",
                         "data": {"results": [], "summary": {}, "ok": bool(ok),
                                  "message": message},
                         "ok": bool(ok)})

    # ---------- 反馈进化：采集 / 统计（M6，DESIGN §7.2③） ----------

    async def _ag_feedback(self, data: dict) -> None:
        """直播中枢那条 AI 回复旁的三个按钮：采纳 / 弃用 / 改写。

        页面把它收到的那条 `feedback` 上下文**原样回传**（不让页面自己拼字段，
        字段口径只在一处定义），这里只做三件事：分派 → 落库（`FeedbackStore`）
        → 回帧。写成功时顺带把最新统计捎回去，反馈进化区免一次往返刷新。

        反馈是旁路观测：任何失败都只提示用户、不反噬直播链路（`FeedbackStore`
        已把 OSError 归一成 `FeedbackError`）。落库是两次小文件追加（<1 ms），
        不占线程池。
        """
        store = self.feedback_store
        if store is None:
            self._ag_feedback_reply(False, "反馈不可用：智能体未装配（开启总开关并配置 LLM Key）")
            return
        kind = str(data.get("kind") or "").strip().lower()
        entry = data.get("feedback")
        if not isinstance(entry, dict) or not entry:
            self._ag_feedback_reply(False, "反馈缺少上下文：请刷新页面后重试")
            return
        try:
            if kind == "adopt":
                record = store.adopt(entry)
                message = "已采纳为好评样本，下一条起就会参考它"
            elif kind == "reject":
                record = store.reject(entry, data.get("reasons"),
                                      note=str(data.get("note") or ""))
                reasons = "、".join(record.get("reasons") or [])
                message = f"已弃用（{reasons}）" if reasons else "已弃用并留档"
            elif kind == "rewrite":
                record = store.rewrite(entry, str(data.get("text") or ""),
                                       note=str(data.get("note") or ""))
                message = "已按你的改写入库，原文已留档"
            else:
                self._ag_feedback_reply(False, f"未知的反馈类型：{kind or '（空）'}")
                return
        except Exception as e:
            msg = str(e) or f"{type(e).__name__}"
            self._log.warning("webui_agent_feedback_failed",
                              {"kind": kind, "error": f"{type(e).__name__}: {e}"})
            self._ag_feedback_reply(False, msg)
            return
        self._log.info("agent_feedback_saved", {"kind": kind,
                                                "id": record.get("id", ""),
                                                "pid": store.pid})
        self._ag_feedback_reply(True, message, record=record, store=store)

    def _ag_feedback_reply(self, ok: bool, message: str, record: dict = None,
                           store=None) -> None:
        payload = {"ok": bool(ok), "message": message,
                   "record": dict(record or {}),
                   "feedback_id": str((record or {}).get("id") or ""),
                   "stats": {}}
        if ok and store is not None:
            try:
                payload["stats"] = store.stats()
            except Exception as e:      # 统计读失败不影响"已入库"这件事
                self._log.warning("webui_agent_feedback_stats_failed",
                                  {"error": f"{type(e).__name__}: {e}"})
        self.toast(message, "success" if ok else "error")
        self._broadcast({"type": "agent_feedback_done", "data": payload,
                         "ok": bool(ok)})

    def _ag_push_feedback_stats(self) -> None:
        """反馈进化区取数（样本总数 / 本周新增 / 采纳率 / 原因标签分布）。"""
        if self.feedback_store is None:
            self._broadcast({"type": "agent_feedback_stats", "ok": False,
                             "data": {"available": False, "stats": {},
                                      "note": "智能体未装配：开启总开关后这里会显示样本统计"}})
            return
        try:
            stats = self.feedback_store.stats()
        except Exception as e:
            self._log.warning("webui_agent_feedback_stats_failed",
                              {"error": f"{type(e).__name__}: {e}"})
            self._broadcast({"type": "agent_feedback_stats", "ok": False,
                             "data": {"available": True, "stats": {},
                                      "note": f"读取样本统计失败：{e}"}})
            return
        self._broadcast({"type": "agent_feedback_stats", "ok": True,
                         "data": {"available": True, "stats": stats, "note": ""}})

    # ---------- 对话调参（M6） ----------

    async def _ag_session_state(self) -> None:
        """调参区初始化 / 改动后刷新：可用性 + 当前值 + 字段表 + 最近快照。

        未装配（开关 OFF / 没配 LLM Key / 还没建存档）时回 `available: false` + 原因，
        页面据此把输入区置灰并显示为什么（V6.7）——**不隐藏**，主播要知道差什么。
        """
        tuner = self.agent_session
        if tuner is None:
            self._broadcast({"type": "agent_session_state", "ok": False,
                             "data": {"available": False,
                                      "reason": "智能体未装配：开启总开关并配置 LLM Key 后可用",
                                      "fields": [], "current": {}, "snapshots": []}})
            return
        try:
            data = tuner.state()
        except Exception as e:
            self._log.warning("webui_agent_session_state_failed",
                              {"error": f"{type(e).__name__}: {e}"})
            self._broadcast({"type": "agent_session_state", "ok": False,
                             "data": {"available": False,
                                      "reason": f"读取调参状态失败：{e}",
                                      "fields": [], "current": {}, "snapshots": []}})
            return
        self._broadcast({"type": "agent_session_state", "ok": bool(data.get("available")),
                         "data": data})

    async def _ag_session_send(self, data: dict) -> None:
        """一句自然语言指令 → 白名单字段改动。

        慢（要过一次 LLM），所以走 task 由调用方 `create_task` 起来，不占收帧循环。
        `SessionTuner.send` 自己把"无 Key / 无存档 / 模型报错"归一成 `ok: false` 的回帧，
        这里只兜没预料到的异常。
        """
        tuner = self.agent_session
        if tuner is None:
            self._ag_session_reply(False, "调参不可用：智能体未装配（开启总开关并配置 LLM Key）")
            return
        text = str(data.get("text") or "")
        try:
            result = await tuner.send(text)
        except Exception as e:
            message = str(e) or f"{type(e).__name__}"
            self._log.warning("webui_agent_session_failed",
                              {"error": f"{type(e).__name__}: {e}"})
            self._ag_session_reply(False, f"调参失败：{message}")
            return
        result = dict(result or {})
        if result.get("changes"):
            self._log.info("agent_session_changed", {
                "fields": [c.get("field") for c in result["changes"]],
                "snapshot": result.get("snapshot_id", "")})
        self._ag_session_reply(bool(result.get("ok")), str(result.get("reply") or ""),
                               result, kind="send")

    async def _ag_session_undo(self, data: dict) -> None:
        """撤销一次调参（不传 id 撤最近一次）：回滚走同一套 setter，注释与存档语义都保留。"""
        tuner = self.agent_session
        if tuner is None:
            self._ag_session_reply(False, "撤销不可用：智能体未装配", kind="undo")
            return
        snapshot_id = str(data.get("id") or "")
        try:
            # undo 是同步的文件读写（含 config.yaml 行级重写）→ 别占事件循环
            result = await asyncio.to_thread(tuner.undo, snapshot_id)
        except Exception as e:
            message = str(e) or f"{type(e).__name__}"
            self._log.warning("webui_agent_session_undo_failed",
                              {"error": f"{type(e).__name__}: {e}"})
            self._ag_session_reply(False, f"撤销失败：{message}", kind="undo")
            return
        result = dict(result or {})
        self._ag_session_reply(bool(result.get("ok")), str(result.get("message") or ""),
                               result, kind="undo")

    def _ag_session_reply(self, ok: bool, message: str, result: dict = None,
                          kind: str = "send") -> None:
        """回帧 + 就近提示；成功时顺带推一帧状态（配置预览与快照列表免一次往返）。

        `agent_session_send` / `agent_session_undo` 两条动作共用：页面按 `kind` 区分
        渲染（聊天气泡 vs 撤销结果行），字段口径只在后端定义一处。
        """
        result = dict(result or {})
        payload = {"kind": kind, "ok": bool(ok), "message": message,
                   "reply": str(result.get("reply") or ""),
                   "changes": list(result.get("changes") or []),
                   "errors": list(result.get("errors") or []),
                   "restored": list(result.get("restored") or []),
                   "snapshot_id": str(result.get("snapshot_id") or "")}
        self._broadcast({"type": "agent_session_reply", "data": payload, "ok": bool(ok)})
        if ok:
            asyncio.create_task(self._ag_session_state())

    async def _safe_set_tts(self, api_key: str, endpoint: str, voice_id: str,
                            test: bool) -> None:
        """MiniMax Key / 直播音色热配置；结果经 vc 广播 + note_system 双通道反馈。

        configurer 返回 (ok, message) 时向音色工坊广播 config_done（前端按钮复位+就近提示）；
        返回 None 兼容旧行为（仅 configurer 内部 note_system）。
        """
        try:
            result = await self.tts_configurer(api_key, endpoint, voice_id, test)
        except Exception as e:
            self._broadcast_vc({"op": "error", "stage": "config",
                                "error": f"{type(e).__name__}: {e}"})
            self._log.warning("webui_set_tts_failed",
                              {"error": f"{type(e).__name__}: {e}"})
            return
        if result:
            ok, message = result
            self.note_system(message)
            if test:
                self.toast("MiniMax 测试连接成功" if ok else f"MiniMax 测试失败：{message}",
                           "success" if ok else "error")
            elif voice_id:
                self.toast("直播音色已切换" if ok else f"切换音色失败：{message}",
                           "success" if ok else "error")
            else:
                self.toast("MiniMax Key 已保存" if ok else f"保存失败：{message}",
                           "success" if ok else "error")
            if api_key.strip() or endpoint.strip() or test:
                # Key/端点保存：广播 config_done 让按钮复位并就近提示；
                # 纯音色切换走快照自愈（voice_id 变化触发音色库重渲染），不打扰 Key 卡片
                self._broadcast_vc({"op": "config_done", "ok": bool(ok), "message": message,
                                    "minimax": self.voice_workshop.minimax_status()})

    async def _safe_set_llm(self, base_url: str, api_key: str, model: str,
                            test: bool) -> None:
        """LLM 配置保存（含可选连通性测试）；结果经 note_system + llm 广播双通道反馈。"""
        try:
            result = await self.llm_configurer(base_url, api_key, model, test)
        except Exception as e:
            result = (False, f"LLM 配置失败: {type(e).__name__}: {e}")
            self._log.warning("webui_set_llm_failed",
                              {"error": f"{type(e).__name__}: {e}"})
        if result:
            ok, message = result
            data = {"op": "config_result", "ok": bool(ok), "message": message}
            if test:
                # 结构化测试结果
                data["test_result"] = bool(ok)
                if ok:
                    data["detail"] = f"模型可达 · API Key 有效 · 响应正常"
                else:
                    # 从错误信息中提取可读原因和修复建议
                    data["detail"] = message
                    if "401" in message or "密钥" in message or "key" in message.lower():
                        data["advice"] = "检查 API Key 是否过期或被吊销，或前往厂商控制台重新生成"
                    elif "404" in message or "模型" in message or "model" in message.lower():
                        data["advice"] = "检查模型名是否准确，部分厂商模型 ID 带版本后缀（如 deepseek-chat）"
                    elif "超时" in message or "timeout" in message.lower() or "网络" in message:
                        data["advice"] = "检查网络连接、代理设置，或确认接口地址是否可达"
                    elif "不是 OpenAI 兼容格式" in message:
                        data["advice"] = "确认 Base URL 是否以 /v1 结尾，且指向 Chat Completions 接口"
                    else:
                        data["advice"] = "确认接口地址、Key、模型名三项均填写正确，或检查厂商服务状态"
            self.note_system(message)
            if test:
                self.toast("LLM 测试连接成功" if ok else f"LLM 测试失败：{message}",
                           "success" if ok else "error")
            else:
                self.toast("LLM 配置已保存" if ok else f"LLM 保存失败：{message}",
                           "success" if ok else "error")
            self._broadcast({"type": "llm", "data": data})

    async def _safe_set_fallback(self, text: str, enabled: bool) -> None:
        """兜底口播配置保存（无连通测试）；结果经 note_system + toast 反馈。"""
        try:
            result = await self.fallback_configurer(text, enabled)
        except Exception as e:
            result = (False, f"兜底配置失败: {type(e).__name__}: {e}")
            self._log.warning("webui_set_fallback_failed",
                              {"error": f"{type(e).__name__}: {e}"})
        ok, message = result
        self.note_system(message)
        self.toast("兜底口播已保存" if ok else f"兜底保存失败：{message}",
                   "success" if ok else "error")

    async def _safe_toggle(self, hotkey) -> None:
        try:
            await hotkey.toggle()
        except Exception as e:
            self._log.error("webui_toggle_failed", {"error": f"{type(e).__name__}: {e}"})
            self.toast(f"暂停/恢复失败：{e}", "error")
            return
        self.toast("已暂停发声" if hotkey.paused else "已恢复发声", "success")

    async def _safe_test_audio(self, device: str) -> None:
        """TTS 试听：合成「音频测试」播放到指定设备；结果广播给前端。"""
        if not device:
            self._broadcast({"type": "audio_device",
                             "data": {"op": "test", "ok": False,
                                      "message": "请先选择输出设备"}})
            self.toast("请先选择输出设备", "error")
            return
        try:
            ok, msg = await self.audio_device_tester(device)
        except Exception as e:
            ok, msg = False, f"试听失败：{type(e).__name__}: {e}"
            self._log.warning("webui_test_audio_failed",
                              {"error": f"{type(e).__name__}: {e}", "device": device})
        self.toast("试听完成" if ok else f"试听失败：{msg}", "success" if ok else "error")
        self._broadcast({"type": "audio_device",
                         "data": {"op": "test", "ok": bool(ok),
                                  "message": msg, "device": device}})

    # ---------- 广播 ----------

    @staticmethod
    def _dumps(message: dict) -> str:
        return json.dumps(message, ensure_ascii=False)

    def _broadcast(self, message: dict) -> None:
        msg = self._dumps(message)
        for outbox in list(self._clients):
            try:
                outbox.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # 慢客户端靠下一秒快照自愈

    def _broadcast_event(self, entry: dict) -> None:
        self._broadcast({"type": "event", "data": entry})
