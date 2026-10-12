"""对话式调参（DESIGN §7.2①，M6）——说人话改配置，改完能撤销。

主播说「口播太频繁了」/「回复再短点」/「加个话题：今天打新区」，这条自然语言经
LLM function calling 映射到**白名单字段**的一次写入：写盘前留快照，写盘后回显
「字段：旧值 → 新值」，不满意一键撤销。

## 三条硬约束

1. **白名单**：模型只能调 `tool_specs()` 里注册的字段（人设语气 / 回复字数上限 /
   口播间隔 / 话题 / 话术样本 / 游戏知识）。工具名不在表里一律拒；参数一律当
   **不可信外部输入**处理（长度截断 + 范围钳制），绝不做"把模型返回的键写进配置"。
2. **写前快照**：`logs/agent_session.json` 保留最近 `MAX_SNAPSHOTS` 次（默认 20）。
   每次改动都带上"怎么还原"，撤销走**同一套 setter**（config 记旧值重写、存档记变更
   分区的原值重写），不做整文件回滚——否则会把主播在别处刚保存的改动一起冲掉。
3. **热生效**：写完立刻调 `applier`（main 注入），让内存 cfg / 提示词 / 调度器按新值走。

## 落点为什么分两处（M6 实测记录里有这条，是**先读代码才发现的缺口**）

| 字段 | 落盘 | 理由 |
|---|---|---|
| 人设语气 / 字数上限 / 话题 / 话术 / 游戏知识 | `profiles/<pid>/profile.yaml` | 存档内容，换存档就该换一套人设 |
| 口播间隔 | **存档** `schedule.interval_s`（权威）+ `config.yaml` `schedule.interval_min`（同步引导值） | 见下 |

间隔这条与 DESIGN §7.2① 的写法（"写回 config.yaml 行级替换"）**不完全一致**：
`config.yaml` 的 `schedule.interval_min` 只在两处被读——`Scheduler.__init__`（构造期默认值）
与 `main._migrate_legacy_pool`（一次性把旧话术池迁进存档）。一旦存档有了话题，
`_apply_script_runtime()` 每次都按 `ScriptRuntime.schedule()`（读存档的 `interval_s`）
重配调度器，**config 里那份就再也不生效了**。所以只写 config 会导致「参数变了、实际没变」
——正好是 V6.5 要防的假生效。现在两处都写：存档那份是热的，config 那份保持与 DESIGN
写法一致、并作为无存档时的引导值。

字数上限同理：唯一权威是存档 `knowledge.max_reply_chars`（`ScriptRuntime.max_reply_chars()`
读它，`AgentBrain` 每次决策现读），`config.yaml` 的 `persona.max_reply_chars` 只是无存档时的兜底。

## 不做的事

- 不碰护栏（敏感词、暂停键、播放串行、`event_enabled`）；工具表里没有这些字段，
  模型喊不出来就改不了。
- 不删除任何东西：只能"改值 / 追加 / 覆盖整块"，删话题、删话术请去话术管理页。
"""
import json
import time
from pathlib import Path

from hub.config import update_schedule_config
from hub.logger import get_logger

# ── 落盘与体量上限 ──
SNAPSHOT_FILE = "agent_session.json"
MAX_SNAPSHOTS = 20
MAX_REQUEST_CHARS = 200          # 一句自然语言指令
MAX_REPLY_CHARS = 600            # 会话回复
MAX_STYLE_CHARS = 80             # 人设语气描述
MAX_TOPIC_TITLE = 40
MAX_TOPIC_TEXT = 500
MAX_SAMPLE_TEXT = 200
MAX_SAMPLE_EXAMPLES = 300
MAX_HIGHLIGHT_CHARS = 500
MAX_HIGHLIGHTS = 10
MAX_TOOL_CALLS = 4               # 一条指令最多落几项改动（防模型刷一地工具调用）

# 与 hub/config.py validate_config 的范围对齐（越界写下去会让应用下次启动校验失败）
INTERVAL_MIN_RANGE = (1, 60)     # 分钟；config 侧校验 1~120，存档侧 1~3600s，取交集
REPLY_CHARS_RANGE = (20, 60)     # 与 _check_number("persona.max_reply_chars", 20, 60) 一致

# 字段清单（页面右侧「配置预览」按这个渲染；key 与工具名一一对应）
FIELDS = (
    {"key": "style", "label": "人设语气", "scope": "profile",
     "unit": "", "hint": "一句话说话风格（不超过 80 字）"},
    {"key": "max_reply_chars", "label": "回复字数上限", "scope": "profile",
     "unit": "字", "hint": "20 ~ 60 字；超过会被截断"},
    {"key": "interval_min", "label": "口播间隔", "scope": "config",
     "unit": "分钟", "hint": f"{INTERVAL_MIN_RANGE[0]} ~ {INTERVAL_MIN_RANGE[1]} 分钟"},
    {"key": "topics_ai", "label": "话题条数", "scope": "profile", "unit": "条",
     "hint": "只增不减，删除请去话术管理页"},
    {"key": "samples_ai", "label": "话术样本", "scope": "profile", "unit": "条",
     "hint": "只增不减，删除请去话术管理页"},
    {"key": "game_intro", "label": "游戏介绍", "scope": "profile",
     "unit": "", "hint": "一句话玩法亮点"},
)


class SessionError(Exception):
    """调参请求无法受理（无 Key / 无存档 / 输入为空）——由调用方回显给主播。"""


# ---------------------------------------------------------------- 工具表

def tool_specs() -> list:
    """OpenAI 风格的工具定义（`tools` 参数）。字段名 = 允许改的东西，多一个都没有。"""
    return [
        {"type": "function", "function": {
            "name": "set_style",
            "description": "改主播的人设语气描述（说话风格）。",
            "parameters": {"type": "object", "properties": {
                "style": {"type": "string",
                          "description": f"一句话风格描述，不超过 {MAX_STYLE_CHARS} 字"}},
                "required": ["style"]}}},
        {"type": "function", "function": {
            "name": "set_max_reply_chars",
            "description": "改单条口播回复的字数上限。嫌太长就调小，嫌太短就调大。",
            "parameters": {"type": "object", "properties": {
                "chars": {"type": "integer",
                          "description": f"{REPLY_CHARS_RANGE[0]} ~ {REPLY_CHARS_RANGE[1]}"}},
                "required": ["chars"]}}},
        {"type": "function", "function": {
            "name": "set_interval_min",
            "description": "改话题口播的间隔分钟数。说「太频繁了」往大调，「太冷清了」往小调。",
            "parameters": {"type": "object", "properties": {
                "minutes": {"type": "integer",
                            "description": f"{INTERVAL_MIN_RANGE[0]} ~ {INTERVAL_MIN_RANGE[1]} 分钟"}},
                "required": ["minutes"]}}},
        {"type": "function", "function": {
            "name": "add_topic",
            "description": "新增一条主播自述话题（轮播用）。",
            "parameters": {"type": "object", "properties": {
                "title": {"type": "string", "description": f"短标题，不超过 {MAX_TOPIC_TITLE} 字"},
                "text": {"type": "string", "description": f"话题正文，不超过 {MAX_TOPIC_TEXT} 字"}},
                "required": ["text"]}}},
        {"type": "function", "function": {
            "name": "add_sample",
            "description": "新增一条问答话术样本（观众这么说 → 主播这么回）。",
            "parameters": {"type": "object", "properties": {
                "examples": {"type": "string",
                             "description": "观众可能的说法，多条用 / 分隔"},
                "reply_text": {"type": "string",
                               "description": f"主播的回复，不超过 {MAX_SAMPLE_TEXT} 字"}},
                "required": ["reply_text"]}}},
        {"type": "function", "function": {
            "name": "set_game_knowledge",
            "description": "改游戏知识（一句话玩法 / 下载引导 / 卖点列表），只改传进来的项。",
            "parameters": {"type": "object", "properties": {
                "game_intro": {"type": "string", "description": "一句话玩法亮点"},
                "download_hint": {"type": "string", "description": "下载引导话术"},
                "highlights": {"type": "array", "items": {"type": "string"},
                               "description": f"卖点列表，最多 {MAX_HIGHLIGHTS} 条"}},
                "required": []}}},
    ]


SYSTEM_PROMPT = """你是「仔仔鼠直播助手」的配置助手，负责把主播的口语化要求翻译成配置改动。

只能改下面这些，其它一律不动、也不要假装能改：
- set_style              人设语气（一句话，不超过 80 字）
- set_max_reply_chars    回复字数上限（20 ~ 60）
- set_interval_min       话题口播间隔（1 ~ 60 分钟）
- add_topic              加一条主播自述话题
- add_sample             加一条问答话术样本
- set_game_knowledge     改游戏介绍 / 下载引导 / 卖点列表

规则：
1. 能确定数值就直接调工具，不要反问；只有要求模糊到定不出数值时才用一句话反问。
2. 主播说「太频繁」「太多」就往大调，说「太冷清」「太少」就往小调，自己拿捏一个合理数字。
3. 一条请求可以调多个工具（如「回复短点、间隔长点」= 两个调用）。
4. 不要编造主播没给过的事实（游戏名、福利、下载方式一律以当前配置为准）。
5. 调完工具后用一句主播听得懂的话说明改了什么（不超过 40 字），别输出 JSON、别念参数名。
"""


# ---------------------------------------------------------------- 工具函数

def _clip(value, limit: int) -> str:
    return str(value if value is not None else "").strip().replace("\n", " ")[:limit]


def _int_arg(value, lo: int, hi: int, field: str) -> int:
    """工具参数转整数并卡范围：模型可能塞 "十分钟" / 999 / 7.5，一律当场拒绝。"""
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        raise SessionError(f"{field}要是整数（收到 {value!r}）") from None
    if n < lo or n > hi:
        raise SessionError(f"{field}需在 {lo} ~ {hi} 之间（收到 {n}）")
    return n


def _parse_arguments(raw) -> dict:
    """`tool_calls[].function.arguments` 是**字符串**形式的 JSON；坏 JSON 直接拒。"""
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip() or "{}"
    try:
        data = json.loads(text)
    except ValueError as e:
        raise SessionError(f"工具参数不是合法 JSON：{e}") from None
    if not isinstance(data, dict):
        raise SessionError("工具参数顶层必须是对象")
    return data


def _minutes(seconds) -> int:
    """秒 → 分钟（四舍五入，至少 1）——回显与 config 同步都用它，两处口径必须一致。"""
    try:
        return max(int(round(float(seconds) / 60.0)), 1)
    except (TypeError, ValueError):
        return 1


# ---------------------------------------------------------------- 会话主体

class SessionTuner:
    """对话调参：`send()` 一句话 → 一次或多次白名单写入 + 快照；`undo()` 回滚。

    注入项（全部可替身，单测不碰网络也不碰真磁盘）：

    | 参数 | 作用 |
    |---|---|
    | `llm` | `hub.agent.llm.AgentLLM`（用它的 `ask_message`）。None / 未配 Key → 不可用 |
    | `store` | `hub.scriptstore.ScriptStore`（读写 `profiles/<pid>/profile.yaml`） |
    | `pid_provider` | `() -> str` 当前激活存档 id（与 `script_rt.profile_id` 同源） |
    | `config_path` | `config.yaml` 路径（写 `schedule.interval_min`） |
    | `applier` | `(changes) -> list[str]` 热生效（main 注入：reload 存档 + 重配调度器） |
    | `cfg` | 内存 cfg（读当前间隔；不传则只从 config_path 读） |
    | `snapshot_path` | 快照文件路径，默认 `logs/agent_session.json` |
    """

    def __init__(self, llm, store, pid_provider, config_path, applier=None,
                 cfg: dict = None, snapshot_path=None, clock=None, log=None,
                 max_snapshots: int = MAX_SNAPSHOTS):
        self._llm = llm
        self._store = store
        self._pid_provider = pid_provider
        self._config_path = Path(config_path) if config_path else None
        self._applier = applier
        self._cfg = cfg if cfg is not None else {}
        self._clock = clock or time.time
        self._log = log or get_logger("agent.session")
        self._max_snapshots = max(1, int(max_snapshots))
        self._snapshot_path = (Path(snapshot_path) if snapshot_path
                               else self._default_snapshot_path(store))

    @staticmethod
    def _default_snapshot_path(store) -> Path:
        from hub import paths
        root = getattr(store, "root", None)
        base = Path(root).parent if root is not None else Path(paths.LOGS_DIR)
        return base / "logs" / SNAPSHOT_FILE

    # ---------- 状态 ----------

    @property
    def available(self) -> bool:
        return not self._unavailable_reason()

    def _unavailable_reason(self) -> str:
        if self._llm is None or not getattr(self._llm, "configured", False):
            return "未配置 LLM Key：对话调参需要大模型把口语要求翻译成配置改动"
        if self._store is None or not self.pid:
            return "还没有可用的直播存档：先在「话术管理」建一个存档再调参"
        return ""

    @property
    def pid(self) -> str:
        try:
            return str(self._pid_provider() or "").strip()
        except Exception as e:
            self._log.warning("session_pid_failed", {"error": f"{type(e).__name__}: {e}"})
            return ""

    def current(self) -> dict:
        """当前值（页面右侧「配置预览」与提示词共用同一份口径）。"""
        pid = self.pid
        out = {"pid": pid, "style": "", "max_reply_chars": REPLY_CHARS_RANGE[0],
               "interval_min": INTERVAL_MIN_RANGE[0], "topics_ai": 0, "samples_ai": 0,
               "game_intro": "", "game_name": ""}
        if pid and self._store is not None:
            try:
                k = self._store.get_profile(pid)["knowledge"]
                out["style"] = str(k.get("style") or "")
                out["max_reply_chars"] = int(k.get("max_reply_chars")
                                             or REPLY_CHARS_RANGE[0])
                out["game_intro"] = str(k.get("game_intro") or "")
                out["game_name"] = str(k.get("game_name") or "")
                p = self._store.get_profile(pid)
                out["topics_ai"] = len(p.get("topics_ai") or [])
                out["samples_ai"] = len(p.get("samples_ai") or [])
                out["interval_min"] = _minutes((p.get("schedule") or {}).get("interval_s", 1800))
            except Exception as e:
                self._log.warning("session_current_failed",
                                  {"pid": pid, "error": f"{type(e).__name__}: {e}"})
        # config 侧兜底：无存档时用 config.yaml 的 interval_min 显示
        if not out["interval_min"]:
            out["interval_min"] = self._config_interval()
        return out

    def state(self) -> dict:
        """页面初始化用：可用性、当前值、字段表、最近快照。"""
        reason = self._unavailable_reason()
        return {"available": not reason, "reason": reason, "fields": list(FIELDS),
                "current": self.current(), "snapshots": self.snapshots()}

    def snapshots(self) -> list:
        """最近快照（新的在前），页面「撤销」按钮按它渲染。"""
        data = self._read_snapshots()
        return list(reversed(data))[:self._max_snapshots]

    # ---------- 快照读写 ----------

    def _read_snapshots(self) -> list:
        try:
            raw = json.loads(self._snapshot_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            self._log.warning("session_snapshot_read_failed",
                              {"path": str(self._snapshot_path),
                               "error": f"{type(e).__name__}: {e}"})
            return []
        items = raw.get("snapshots") if isinstance(raw, dict) else None
        return [x for x in (items or []) if isinstance(x, dict)]

    def _write_snapshots(self, items: list) -> None:
        items = items[-self._max_snapshots:]
        payload = {"version": 1, "snapshots": items}
        try:
            self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            self._snapshot_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            self._log.warning("session_snapshot_write_failed",
                              {"path": str(self._snapshot_path),
                               "error": f"{type(e).__name__}: {e}"})

    # ---------- config 侧读写 ----------

    def _config_interval(self) -> int:
        """当前间隔（分钟）：内存 cfg 优先，其次 config.yaml（都是"引导值"口径）。"""
        sc = (self._cfg or {}).get("schedule") or {}
        if sc:
            try:
                return max(int(round(float(sc.get("interval_min", 7)))), 1)
            except (TypeError, ValueError):
                pass
        if self._config_path is not None:
            try:
                from hub.config import load_config
                return max(int(round(float(
                    (load_config(str(self._config_path)).get("schedule") or {})
                    .get("interval_min", 7)))), 1)
            except Exception:
                pass
        return 7

    def _write_config_interval(self, minutes: int) -> None:
        if self._config_path is None:
            return
        update_schedule_config(str(self._config_path), interval_min=int(minutes))
        sc = self._cfg.setdefault("schedule", {}) if isinstance(self._cfg, dict) else None
        if isinstance(sc, dict):
            sc["interval_min"] = int(minutes)

    # ---------- 主流程 ----------

    async def send(self, text) -> dict:
        """把一句话指令交给模型 → 执行白名单工具 → 落快照 → 回显。

        返回：`{ok, reply, changes: [{field, path, old, new}], errors: [...], snapshot_id}`
        - `ok=False` 只在"整条请求没法受理"时出现（无 Key / 无存档 / 输入为空 / 模型报错）；
        - 单个工具调用失败（越界、坏 JSON、未知工具）记进 `errors` 并继续执行其余调用，
          因为模型常一次纠多个字段，不该因为一项越界就把另一些合理改动丢掉。
        """
        request = _clip(text, MAX_REQUEST_CHARS)
        if not request:
            return {"ok": False, "reply": "想调什么？比如「口播太频繁了」「回复短一点」。",
                    "changes": [], "errors": ["指令为空"], "snapshot_id": ""}
        reason = self._unavailable_reason()
        if reason:
            return {"ok": False, "reply": reason, "changes": [], "errors": [reason],
                    "snapshot_id": ""}

        try:
            msg = await self._llm.ask_message(
                SYSTEM_PROMPT, self._user_prompt(request), tools=tool_specs())
        except Exception as e:
            message = f"{type(e).__name__}: {e}"
            self._log.warning("session_llm_failed", {"error": message})
            return {"ok": False, "reply": f"模型没连上：{message}", "changes": [],
                    "errors": [message], "snapshot_id": ""}

        calls = list(msg.get("tool_calls") or [])[:MAX_TOOL_CALLS]
        content = str(msg.get("content") or "").strip()
        if not calls:
            # 纯对话（如"你能改什么"）——不改任何东西，也就没有快照可撤
            return {"ok": True, "reply": content or "这条我没听出要改什么，换个说法试试。",
                    "changes": [], "errors": [], "snapshot_id": ""}

        ops, errors = [], []
        for call in calls:
            try:
                ops.append(self._resolve(call))
            except SessionError as e:
                errors.append(str(e))
        if not ops:
            return {"ok": False,
                    "reply": content or "模型给的改动不合规，我没有执行：" + "；".join(errors),
                    "changes": [], "errors": errors, "snapshot_id": ""}

        try:
            changes, restore = self._apply(ops)
        except Exception as e:
            message = f"{type(e).__name__}: {e}"
            self._log.warning("session_apply_failed", {"error": message})
            return {"ok": False, "reply": f"写配置失败：{message}", "changes": [],
                    "errors": errors + [message], "snapshot_id": ""}

        if not changes:
            # 全是不需要动（值本来就一样）——不留快照，否则"撤销"会撤出一次空操作
            return {"ok": True, "reply": content or self._echo(ops, changes),
                    "changes": [], "errors": errors, "snapshot_id": ""}

        snapshot_id = self._save_snapshot(request, content, changes, restore)
        self._hot_apply(changes)
        reply = content or self._echo(ops, changes)
        return {"ok": True, "reply": reply, "changes": changes, "errors": errors,
                "snapshot_id": snapshot_id}

    def _user_prompt(self, request: str) -> str:
        cur = self.current()
        lines = ["【当前配置】",
                 f"- 人设语气：{cur['style'] or '（空）'}",
                 f"- 回复字数上限：{cur['max_reply_chars']} 字",
                 f"- 口播间隔：{cur['interval_min']} 分钟",
                 f"- 话题条数：{cur['topics_ai']} / 话术样本：{cur['samples_ai']} 条",
                 f"- 游戏名：{cur['game_name'] or '（未填）'}"]
        if cur["game_intro"]:
            lines.append(f"- 游戏介绍：{cur['game_intro']}")
        lines += ["", "【主播说】" + request]
        return "\n".join(lines)

    # ---------- 工具 → 操作 ----------

    def _resolve(self, call: dict) -> dict:
        """一条 tool_call → 一个操作描述（只做校验，不写盘）。

        操作形状：`{tool, field, path, scope, old, new, kind}`，其中
        `old == new` 时 `kind="noop"`（不写、不留快照，回显"已经是"）。
        """
        if not isinstance(call, dict):
            raise SessionError("工具调用不是对象")
        fn = call.get("function") or {}
        name = str(fn.get("name") or "").strip()
        args = _parse_arguments(fn.get("arguments"))
        cur = self.current()
        pid = self.pid

        if name == "set_style":
            style = _clip(args.get("style"), MAX_STYLE_CHARS)
            if not style:
                raise SessionError("人设语气不能为空")
            return self._op(name, "人设语气", "profile.knowledge.style", "profile",
                            cur["style"], style, pid)

        if name == "set_max_reply_chars":
            n = _int_arg(args.get("chars"), *REPLY_CHARS_RANGE, "回复字数上限")
            return self._op(name, "回复字数上限", "profile.knowledge.max_reply_chars",
                            "profile", cur["max_reply_chars"], n, pid)

        if name == "set_interval_min":
            n = _int_arg(args.get("minutes"), *INTERVAL_MIN_RANGE, "口播间隔")
            return self._op(name, "口播间隔", "schedule.interval_s", "config",
                            cur["interval_min"], n, pid)

        if name == "add_topic":
            text = _clip(args.get("text"), MAX_TOPIC_TEXT)
            if not text:
                raise SessionError("话题正文不能为空")
            title = _clip(args.get("title"), MAX_TOPIC_TITLE) or text[:12]
            return self._op(name, "话题", "profile.topics_ai", "profile",
                            f"{cur['topics_ai']} 条", title, pid,
                            payload={"title": title, "text": text})

        if name == "add_sample":
            reply = _clip(args.get("reply_text"), MAX_SAMPLE_TEXT)
            if not reply:
                raise SessionError("话术回复不能为空")
            examples = _clip(args.get("examples"), MAX_SAMPLE_EXAMPLES)
            return self._op(name, "话术样本", "profile.samples_ai", "profile",
                            f"{cur['samples_ai']} 条", reply, pid,
                            payload={"examples": [x.strip() for x in examples.split("/")
                                                  if x.strip()][:6],
                                     "reply_text": reply})

        if name == "set_game_knowledge":
            payload = {}
            if args.get("game_intro") is not None:
                payload["game_intro"] = _clip(args.get("game_intro"), MAX_TOPIC_TEXT)
            if args.get("download_hint") is not None:
                payload["download_hint"] = _clip(args.get("download_hint"), MAX_TOPIC_TEXT)
            if args.get("highlights") is not None:
                items = args.get("highlights")
                if not isinstance(items, list):
                    raise SessionError("卖点列表要是数组")
                payload["highlights"] = [_clip(x, MAX_HIGHLIGHT_CHARS)
                                         for x in items[:MAX_HIGHLIGHTS] if _clip(x, 1)]
            if not payload or all(not v for v in payload.values()):
                raise SessionError("游戏知识：没有可改的项（要传 game_intro / download_hint / highlights）")
            pairs = (("游戏介绍", "game_intro"), ("下载引导", "download_hint"),
                     ("卖点", "highlights"))
            label = "、".join(name for name, key in pairs if payload.get(key))
            return self._op(name, f"游戏知识（{label}）", "profile.knowledge", "profile",
                            "原值", "新值", pid, payload=payload)

        raise SessionError(f"未知工具：{name or '（空）'}")

    @staticmethod
    def _op(tool: str, field: str, path: str, scope: str, old, new, pid: str,
            payload: dict = None) -> dict:
        return {"tool": tool, "field": field, "path": path, "scope": scope,
                "old": old, "new": new, "pid": pid, "payload": payload or {},
                "kind": "noop" if (payload is None and old == new) else "write"}

    # ---------- 写盘 ----------

    def _apply(self, ops: list) -> tuple:
        """执行操作，返回 `(changes, restore)`。

        同一次请求里的多个操作**共用一份快照**：撤销就该一次回到动手之前。
        """
        changes, restore = [], {}
        profile_ops = [o for o in ops if o["scope"] == "profile" and o["kind"] == "write"]
        config_ops = [o for o in ops if o["scope"] == "config" and o["kind"] == "write"]

        if profile_ops:
            pid = profile_ops[0]["pid"]
            data = self._store.get_profile(pid)
            touched = {}
            for op in profile_ops:
                branch = op["path"].split(".")[1]           # profile.<branch>...
                touched.setdefault(branch, json.loads(json.dumps(data.get(branch))))
                data, shown = self._apply_profile_op(data, op)
                changes.append({"field": op["field"], "path": op["path"],
                                "old": op["old"], "new": shown,
                                "scope": "profile", "note": "存档"})
            # 存"变更分区的原值"而不是整档：撤销时只回写被动过的分区，
            # 不碰主播刚在话术管理页保存的其它改动
            restore["profile"] = {"pid": pid, "sections": touched}
            self._store.save_profile(pid, data)

        if config_ops:
            # config.yaml 只是"引导值"，有存档时真正生效的是存档的 schedule.interval_s
            # ——两处一起写，否则会出现「参数变了、实际没变」的假生效（V6.5 要防的正是这个）。
            minutes_old = self._config_interval()
            pid = self.pid
            profile_data = None
            if pid:
                profile_data = self._store.get_profile(pid)
                restore.setdefault("profile", {"pid": pid, "sections": {}})
                restore["profile"]["sections"].setdefault(
                    "schedule", json.loads(json.dumps(profile_data.get("schedule"))))
            for op in config_ops:
                minutes_new = int(op["new"])
                self._write_config_interval(minutes_new)
                if profile_data is not None:
                    profile_data.setdefault("schedule", {})["interval_s"] = minutes_new * 60
                changes.append({"field": op["field"], "path": op["path"],
                                "old": f"{op['old']} 分钟", "new": f"{minutes_new} 分钟",
                                "scope": "config", "note": "config.yaml + 存档轮播"})
            if profile_data is not None:
                self._store.save_profile(pid, profile_data)
            restore["config"] = {"schedule.interval_min": minutes_old}

        return changes, restore

    def _apply_profile_op(self, data: dict, op: dict) -> tuple:
        """把一项操作写进存档 dict（内存），返回 (data, 回显用新值)。"""
        p = data
        if op["path"] == "profile.knowledge.style":
            p["knowledge"]["style"] = op["new"]
            return p, op["new"]
        if op["path"] == "profile.knowledge.max_reply_chars":
            p["knowledge"]["max_reply_chars"] = int(op["new"])
            return p, f"{op['new']} 字"
        if op["path"] == "profile.topics_ai":
            from hub.scriptstore import _new_id
            item = {"id": _new_id("t"), "title": op["payload"]["title"],
                    "text": op["payload"]["text"], "audios": {}, "enabled": True}
            p.setdefault("topics_ai", []).append(item)
            return p, f"{op['payload']['title']}（新增）"
        if op["path"] == "profile.samples_ai":
            from hub.scriptstore import _new_id
            item = {"id": _new_id("s"), "type_name": op["payload"]["reply_text"][:12],
                    "examples": op["payload"]["examples"],
                    "reply_text": op["payload"]["reply_text"],
                    "audios": {}, "enabled": True}
            p.setdefault("samples_ai", []).append(item)
            return p, f"{op['payload']['reply_text']}（新增）"
        if op["path"] == "profile.knowledge":
            for key, value in op["payload"].items():
                p["knowledge"][key] = value
            return p, "已更新"
        raise SessionError(f"未知落点：{op['path']}")

    def _echo(self, ops: list, changes: list) -> str:
        """模型没给提醒语时的兜底回显：一律「字段：旧值 → 新值」。"""
        lines = [self._echo_line(c) for c in changes]
        noop = [o for o in ops if o["kind"] == "noop"]
        lines += [f"{o['field']}：已经是 {o['old']}，无需改动" for o in noop]
        return "；".join(lines) if lines else "没有需要改的地方。"

    @staticmethod
    def _echo_line(change: dict) -> str:
        return f"{change['field']}：{change['old']} → {change['new']}"

    def _save_snapshot(self, request: str, reply: str, changes: list,
                       restore: dict) -> str:
        items = self._read_snapshots()
        # id 必须唯一：`ss_<毫秒>` 在"注入时钟"或同一毫秒内连改两次时会撞车，
        # 撞了之后 undo(id) 会撤错那一条（先撞先得）。
        base = "ss_%d" % int(self._clock() * 1000)
        used = {str(it.get("id")) for it in items}
        snap_id, n = base, 1
        while snap_id in used:
            snap_id = f"{base}_{n}"
            n += 1
        items.append({"id": snap_id, "ts": int(self._clock()), "request": request,
                      "reply": reply, "changes": changes, "restore": restore})
        self._write_snapshots(items)
        self._log.info("session_changed", {
            "snapshot": snap_id, "changes": [c["path"] for c in changes]})
        return snap_id

    def _hot_apply(self, changes: list) -> list:
        """热生效：main 注入的 applier 负责，失败只记日志——配置已落盘，不该回滚。"""
        if self._applier is None:
            return []
        try:
            notes = list(self._applier(changes) or [])
        except Exception as e:
            self._log.warning("session_hot_apply_failed",
                              {"error": f"{type(e).__name__}: {e}"})
            return []
        if notes:
            self._log.info("session_hot_applied", {"notes": notes})
        return notes

    # ---------- 撤销 ----------

    def undo(self, snapshot_id: str = "") -> dict:
        """回滚一次改动；不传 id 就撤最近一次。

        撤销后该快照**从文件里移除**——它已经被消费，留着会让"再撤一次"把同一改动
        又回滚一遍（而那时的"旧值"已经是回滚后的值，语义就乱了）。
        """
        items = self._read_snapshots()
        if not items:
            return {"ok": False, "message": "没有可撤销的改动", "restored": []}
        target = None
        if snapshot_id:
            for it in items:
                if str(it.get("id")) == str(snapshot_id):
                    target = it
                    break
            if target is None:
                return {"ok": False, "message": "找不到该快照（可能已撤销过）",
                        "restored": []}
        else:
            target = items[-1]

        restored, errors = [], []
        restore = target.get("restore") or {}
        prof = restore.get("profile") or {}
        if prof.get("pid"):
            try:
                data = self._store.get_profile(prof["pid"])
                for branch, value in (prof.get("sections") or {}).items():
                    data[branch] = value
                    restored.append(f"{branch} 回到改动前")
                self._store.save_profile(prof["pid"], data)
            except Exception as e:
                errors.append(f"存档回滚失败：{type(e).__name__}: {e}")

        cfg_restore = restore.get("config") or {}
        if "schedule.interval_min" in cfg_restore:
            try:
                old = int(cfg_restore["schedule.interval_min"])
                self._write_config_interval(old)
                restored.append(f"口播间隔 回到 {old} 分钟")
            except Exception as e:
                errors.append(f"间隔回滚失败：{type(e).__name__}: {e}")
        # 存档侧的 interval_s 不在这里补写：它已经躺在 restore.profile.sections["schedule"] 里，
        # 由上面那段按**原值**还原。要是这里再按"旧分钟数×60"补一次，遇到非整分钟的
        # 原值（如 100 秒被四舍五入成 2 分钟 → 写回 120 秒）就把原值改坏了。

        if errors and not restored:
            return {"ok": False, "message": "；".join(errors), "restored": []}

        self._write_snapshots([it for it in items if it is not target])
        self._hot_apply([{"scope": "config", "path": "schedule.interval_s"},
                         {"scope": "profile", "path": "*"}])
        message = "已撤销：" + "；".join(restored + errors) if (restored or errors) \
            else "这次改动没有需要还原的内容"
        self._log.info("session_undone", {"snapshot": target.get("id"),
                                          "restored": restored, "errors": errors})
        return {"ok": True, "message": message, "restored": restored,
                "snapshot_id": str(target.get("id") or ""), "changes": target.get("changes") or []}


__all__ = ["FIELDS", "INTERVAL_MIN_RANGE", "MAX_SNAPSHOTS", "REPLY_CHARS_RANGE",
           "SNAPSHOT_FILE", "SYSTEM_PROMPT", "SessionError", "SessionTuner",
           "tool_specs"]
