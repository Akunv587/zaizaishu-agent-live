"""事件路由器（PROMPTS 步骤 2.2~2.4，DESIGN §4.2/§6.2；PRD FR-1/FR-2/FR-3）。

管道：事件 → 输入侧敏感词检查（input_filter，步骤 4.1 接词库）→
事件处理器（chat 聚合 / gift 连击 / follow·fansclub 冷却加概率 / enter 开关）→
生成任务 → on_task 回调（步骤 2.5 起接优先级队列）。

确定性设计（测试不真实等待、结果确定）：时钟、随机源、相似度函数均为构造注入。
handle_event 全程同步不阻塞：chat 任务的 LLM 文本生成（步骤 3.1 注入 reply_builder）
经 asyncio 任务异步补齐后投递，期间不挡住礼物等高优先级事件。

双模式（M3 / DESIGN §4.1、§5.1）：
- `rules`（出厂默认）：上面这套确定性管道——聚合窗口、连击状态机、冷却加概率。
- `agent`：管道只做「收事件 + 输入侧敏感词闸门」，其余判断交给智能体——
  chat 不再聚合、不再按概率丢噪声，逐条产出单条事件上下文；
  gift 不再走连击状态机，逐条礼物事件产出；
  follow / fansclub 不再有冷却与概率，事件来一条算一条。
  两种模式都保留：优先级排序、interrupt 头插、逐类事件开关、F9 暂停。
  `set_mode()` 热切换（V3.2），切换前把旧模式的中间态一次性产出（窗口/连击/
  冷却积压），不留半截状态到新模式。
"""
import asyncio
import inspect
import random as _random
import re
import time

from hub.config import agent_settings
from hub.events import LiveEvent, SpeakTask
from hub.logger import get_logger

SIM_THRESHOLD = 0.6        # 相似弹幕判定阈值（PRD FR-1）
GIFT_TIER_MID = 100        # 礼物分档阈值（代码常量，DESIGN §5.2 注）
GIFT_TIER_HIGH = 1000
SCAN_INTERVAL_S = 0.25     # 后台扫描周期：窗口到期与冷却积压
GIFT_SCAN_INTERVAL_S = 0.5  # 礼物状态表扫描周期（DESIGN §6.2：500ms）

MODE_RULES = "rules"       # 规则管道（出厂默认，V3.1）
MODE_AGENT = "agent"       # 智能体接管
MODES = (MODE_RULES, MODE_AGENT)

_GIFT_TEXT_PLACEHOLDER = "[待生成]感谢 {user} 的 {gift} x{count}"
_WELCOME_TEXT_PLACEHOLDER = "[待生成]欢迎 {user}"

# agent 模式各事件类型的默认优先级（决策未给 priority 时用）：与规则模式的档位对齐
# —— 礼物 P0（中/大额本就插队），弹幕/关注/粉丝团/进场 P1
_AGENT_DEFAULT_PRIORITY = {"chat": 1, "gift": 0, "follow": 1, "fansclub": 1, "enter": 1}

_SYMBOL_RE = re.compile(r"[\W_]+", re.UNICODE)


def _char_bigrams(text: str) -> frozenset:
    t = re.sub(r"\s+", "", str(text)).lower()
    if len(t) < 2:
        return frozenset()
    return frozenset(t[i:i + 2] for i in range(len(t) - 1))


def default_similarity(a: str, b: str) -> float:
    """归一化（去空白、转小写）后字符 2-gram 集合重合率（Jaccard）。"""
    ga, gb = _char_bigrams(a), _char_bigrams(b)
    if not ga or not gb:
        return 0.0
    union = len(ga | gb)
    return len(ga & gb) / union if union else 0.0


def is_noise_text(text: str) -> bool:
    """去空白后长度小于 2，或去除符号/emoji 后为空 → 无意义弹幕。"""
    stripped = (text or "").strip()
    if len(stripped) < 2:
        return True
    return _SYMBOL_RE.sub("", stripped) == ""


def normalize_mode(mode) -> str:
    """模式名归一化：只认 rules / agent，非法值（含 None）一律回落 rules。

    回落到规则模式是安全方向：管道行为与旧版一致，不会因为配置写错
    就把整场直播交给智能体。
    """
    text = str(mode or "").strip().lower()
    return text if text in MODES else MODE_RULES


def gift_tier(value) -> str:
    """礼物价值 → 档位（low / mid / high）。

    单点定义，供三处共用：规则模式的逐档模板开关与插队判定、agent 模式下
    交给智能体的档位标注、以及 M5 试玩调试手工构造礼物事件时的档位标注。
    试玩若另算一套阈值，页面上就会看到"同一个 520 的礼物，试玩说中额、直播说大额"。
    """
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        v = 0.0
    if v < GIFT_TIER_MID:
        return "low"
    if v < GIFT_TIER_HIGH:
        return "mid"
    return "high"


class Router:
    def __init__(self, on_task, config: dict = None, stats=None, input_filter=None,
                 clock=None, random=None, similarity=None,
                 reply_builder=None, template_renderer=None, log=None,
                 mode: str = MODE_RULES):
        self._on_task = on_task
        self._stats = stats
        self._log = log or get_logger("router")
        self._clock = clock or time.time
        self._random = random or _random
        self._similarity = similarity or default_similarity
        self._input_filter = input_filter or (lambda text: False)
        # 步骤 3.1 注入：async (context) -> str，chat 任务的 LLM 话术生成
        self._reply_builder = reply_builder
        # 步骤 3.3 注入：sync (source, fields) -> str，礼物/欢迎本地模板渲染
        self._template_renderer = template_renderer
        # M3 模式（rules / agent）：出厂规则模式，main 依 agent.enabled 决定
        self._mode = normalize_mode(mode)
        # M4 接管钩子：agent 模式下 chat 单条上下文的决策来源（见 set_agent_handler）
        self._agent_handler = None

        cfg = config or {}
        ev = cfg.get("events", {})
        self._cfg = cfg

        chat = ev.get("chat", {}) or {}
        self._chat_enabled = bool(chat.get("enabled", True))
        self._merge_window_s = float(chat.get("merge_window_s", 3))
        self._ignore_noise_p = float(chat.get("ignore_noise_p", 0.2))

        gift = ev.get("gift", {}) or {}
        self._gift_enabled = bool(gift.get("enabled", True))
        self._combo_timeout_s = float(gift.get("combo_timeout_s", 5))

        self._cooldown_cfg = {}
        for kind, default_p in (("follow", 0.6), ("fansclub", 0.8)):
            kc = ev.get(kind, {}) or {}
            self._cooldown_cfg[kind] = {
                "enabled": bool(kc.get("enabled", True)),
                "cooldown_s": float(kc.get("cooldown_s", 30)),
                "probability": float(kc.get("probability", default_p)),
            }
        self._enter_enabled = bool((ev.get("enter", {}) or {}).get("enabled", False))
        # 事件模板库逐类开关（存档 event_enabled，两方案共用）：缺省全部放行，
        # 由 main._apply_script_runtime 经 set_event_enabled 注入；test 事件绕过
        self._event_enabled: dict = {}

        # chat 聚合窗口：{"end": 到期时刻, "groups": [组]}
        self._window = None
        self._chat_generating = 0  # 在途 LLM 回复生成计数（话题调度让位用）
        # 礼物连击状态表：(user, gift_name) -> 状态 dict
        self._combos: dict = {}
        # follow / fansclub 冷却状态
        self._cooldowns = {
            kind: {"until": 0.0, "pending_count": 0, "pending_user": ""}
            for kind in self._cooldown_cfg
        }
        # 在途异步产出（chat 的 LLM 生成、异步 on_task），供 drain 等待
        self._inflight: list = []
        self._stopped = False
        self._paused = False

    # ---------- 对外接口 ----------

    def handle_event(self, ev: LiveEvent) -> None:
        """事件入口：同步、不阻塞。按类型分派到各处理器。"""
        if ev is None:
            return
        if self._paused:
            # 暂停语义（PROMPTS 步骤 4.4）：事件照收、只记录不产出任务
            self._log.info("paused_event_recorded", {"type": ev.type, "user": ev.user})
            return
        now = self._clock()
        self.scan(now)
        handler = getattr(self, f"_handle_{ev.type}", None)
        if handler is None:
            self._log.debug("event_type_ignored", {"type": ev.type})
            return
        handler(ev, now)

    def scan(self, now: float = None) -> None:
        """一次巡检：窗口到期产出、连击超时结算、冷却期结束的积压补播。

        由 handle_event 惰性触发 + run() 周期触发，两条路径幂等。
        """
        if now is None:
            now = self._clock()
        self._flush_expired_window(now)
        self._settle_due_gifts(now)
        self._flush_cooldown_pending(now)

    async def run(self) -> None:
        """后台扫描协程（DESIGN §6.2：统一扫描，不为每个键单独起定时器）。"""
        self._log.info("router_scan_started")
        try:
            while not self._stopped:
                self.scan()
                await asyncio.sleep(SCAN_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        self._stopped = True

    def set_reply_builder(self, builder) -> None:
        """热更新：控制台保存 LLM 配置后替换 chat 话术生成器（None = 停用弹幕回复）。"""
        self._reply_builder = builder
        self._log.info("reply_builder_swapped", {"active": builder is not None})

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._log.info("router_pause_changed", {"paused": paused})

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode) -> str:
        """热切换 rules / agent（V3.2，无需重启）。

        切换前先把旧模式的中间态一次性产出——已聚合的弹幕组按原管道结算、
        未到期的礼物连击立即结算、冷却积压立即补播——避免半截状态跨模式
        泄漏（agent 模式不认识聚合窗口与连击表）。返回归一化后的模式名。
        """
        new_mode = normalize_mode(mode)
        if new_mode == self._mode:
            return self._mode
        old = self._mode
        self._flush_intermediate(self._clock())
        self._mode = new_mode
        self._log.info("router_mode_changed", {"from": old, "to": new_mode})
        return self._mode

    def set_agent_handler(self, handler) -> None:
        """M4 接管钩子：agent 模式下 chat 单条事件上下文的决策来源。

        handler 与 reply_builder 同契约——`(context) -> str | dict | None`，
        可为同步或 awaitable（dict 支持 text/audio_path/emotion/speed）。
        未注入时 agent 模式的 chat 仍走 reply_builder（开关打开但智能体尚未
        接管的降级路径），不会因缺钩子而丢弹幕。传 None 撤下钩子。
        """
        self._agent_handler = handler
        self._log.info("agent_handler_swapped", {"active": handler is not None})

    @property
    def agent_attached(self) -> bool:
        """接管钩子是否已注入（是否**真在接管**还需 mode 也是 agent，见 `_agent_active`）。"""
        return self._agent_handler is not None

    def _agent_active(self) -> bool:
        """智能体此刻是否真的在接管：**模式与钩子同时满足**。

        两个条件缺一不可——只切 `mode` 而未注入钩子是 M3 的降级路径（逐条产出但
        仍由本地模板/`reply_builder` 生成文本）；注入了钩子但模式仍是 rules 时，
        规则链路的聚合/连击/冷却必须原样生效，不能把事件偷给智能体。
        """
        return self._mode == MODE_AGENT and self.agent_attached

    def set_event_enabled(self, mapping: dict | None) -> None:
        """热更新：话术存档/方案切换后注入逐类事件开关（gift_low/mid/high、
        follow/fansclub/enter）；None 或空恢复全开。enter 缺省回退到 config 开关。"""
        self._event_enabled = dict(mapping or {})
        self._log.info("event_enabled_swapped", {"enabled": self._event_enabled})

    async def drain(self, timeout: float = 10.0) -> None:
        """等待全部在途异步产出完成（测试与关停前排空用）。"""
        pending = [t for t in self._inflight if not t.done()]
        self._inflight = [t for t in self._inflight if not t.done()]
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), timeout=timeout)

    def finalize(self) -> None:
        """关停前排空：未到期窗口、未结算连击、冷却积压一次性产出。"""
        self._flush_intermediate(self._clock())

    def _flush_intermediate(self, now: float) -> None:
        """把中间态一次性产出（关停前与模式热切换前共用）。"""
        self._flush_expired_window(now + max(self._merge_window_s, self._combo_timeout_s, 1.0) * 10)
        for key in list(self._combos.keys()):
            self._settle_gift(key, now)
        self._flush_cooldown_pending(now + 1e9)

    # ---------- chat：聚合窗口（PRD FR-1） ----------

    def _handle_chat(self, ev: LiveEvent, now: float) -> None:
        if not self._chat_enabled:
            return
        text = (ev.content or "").strip()
        # 输入侧敏感词闸门：两种模式都在聚合/决策之前拦下（V3.4）
        if self._input_filter(text):
            self._inc("blocked_input")
            self._log.info("chat_blocked_input", {"user": ev.user, "content": text})
            return

        if self._mode == MODE_AGENT:
            # agent 模式：不聚合、不按概率丢噪声，逐条产出单条事件上下文
            self._dispatch_context(self._single_context(ev, text))
            return

        if is_noise_text(text) and self._random.random() < self._ignore_noise_p:
            self._inc("noise_ignored")
            self._log.debug("chat_noise_ignored", {"user": ev.user, "content": text})
            return

        if self._window is None:
            self._window = self._new_window(now)
        for group in self._window["groups"]:
            if any(self._similarity(text, member[1]) >= SIM_THRESHOLD
                   for member in group["members"]):
                group["members"].append((ev.user, text))
                group["count"] += 1
                self._log.debug("chat_merged", {"user": ev.user, "group_size": group["count"]})
                return
        self._window["groups"].append({
            "user": ev.user, "content": text, "ts": ev.ts,
            "members": [(ev.user, text)], "count": 1, "test": bool(ev.test),
        })
        self._log.debug("chat_group_opened", {"user": ev.user, "content": text})

    def _new_window(self, now: float) -> dict:
        return {"end": now + self._merge_window_s, "groups": []}

    def _flush_expired_window(self, now: float) -> None:
        window = self._window
        if window is None or now < window["end"]:
            return
        self._window = None
        for group in window["groups"]:
            self._finalize_chat_group(group)
        if window["groups"]:
            self._log.info("chat_window_flushed", {"groups": len(window["groups"])})

    @staticmethod
    def _chat_context(user: str, content: str, members, count: int,
                      ts, test) -> dict:
        """chat 事件上下文（供合并组与 agent 单条共用）。"""
        return {
            "type": "chat",
            "user": user,
            "content": content,
            "merged_count": count,
            "members": list(members),
            "event_ts": ts,
            "test": bool(test),
        }

    def _single_context(self, ev: LiveEvent, text: str) -> dict:
        """agent 模式的单条事件上下文：一条弹幕就是一组（merged_count=1）。"""
        return self._chat_context(ev.user, text, [(ev.user, text)], 1, ev.ts, ev.test)

    def _event_context(self, ev: LiveEvent, now: float, **extra) -> dict:
        """agent 模式的单条事件上下文（弹幕以外的类型共用，M4）。

        与 `_single_context` 同一形状，额外带类型专属字段（如礼物的名称/数量/价值/
        所属档位），让智能体有依据判断"这礼物值不值得谢、谢到什么程度"。
        `event_ts` 取源事件时间戳（与 `_chat_context` 同口径）——agent 模式下队列
        不判过期，该字段只服务决策日志与前端展示。
        """
        context = {
            "type": ev.type,
            "user": ev.user,
            "content": ev.content or "",
            "merged_count": 1,
            "members": [(ev.user, ev.content or "")],
            "event_ts": ev.ts or now,
            "test": bool(ev.test),
        }
        context.update(extra)
        return context

    def _gift_context(self, state: dict, now: float, tier: str) -> dict:
        """礼物事件上下文（agent 模式逐条产出用）。

        `state` 由 `_handle_gift` 的 agent 分支按单条事件构造（含 `ts`）；
        规则模式连击结算传入的 state 没有 `ts`，退回 `now`。
        """
        return {
            "type": "gift",
            "user": state["user"],
            "content": "",
            "merged_count": 1,
            "members": [],
            "event_ts": float(state.get("ts") or now),
            "test": bool(state.get("test", False)),
            "gift_name": str(state.get("gift_name") or ""),
            "gift_count": int(state.get("count") or 1),
            "gift_value": int(state.get("value") or 0),
            "tier": tier,
        }

    def _dispatch_context(self, context: dict) -> None:
        """agent 模式的统一出口（五种事件类型共用，M4 起）。

        - 未注入智能体处理器：弹幕回落既有 `reply_builder` 路径（M3 的降级路径，
          V3.2/V3.4 护栏不变）；其余类型不该走到这里，调用方一律以
          `_agent_active()` 守卫，故只记一条 warning 兜底。
        - 处理器异常 → 旧版兜底口播（决策容错链的末级，实现只有这一处）。
        - 返回 awaitable → 调度后台补齐。**弹幕**计入 `_chat_generating`
          （`has_pending_reply` 的话题调度让位判据），礼物/关注不计入——它们不是
          "有待回复的弹幕"，且 agent 模式下 P2 让位本就放开（DESIGN §六 第 9 条）。
        """
        handler = self._agent_handler
        if handler is None:
            if str(context.get("type") or "chat") == "chat":
                self._finalize_chat_context(context)
            else:
                self._log.warning("agent_handler_missing",
                                  {"type": context.get("type")})
            return
        try:
            result = handler(context)
        except Exception as e:  # 智能体自身异常 → 旧版兜底口播（M4 决策三级容错末级）
            self._log.warning("agent_handler_error", {"error": f"{type(e).__name__}: {e}"})
            self._inc("agent_error")
            self._fallback_reply(context)
            return
        if inspect.isawaitable(result):
            counted = str(context.get("type") or "chat") == "chat"
            if counted:
                self._chat_generating += 1
            self._schedule(self._finalize_chat_await(result, context,
                                                     count_pending=counted))
        else:
            self._emit_agent_result(context, result)

    def _finalize_chat_group(self, group: dict) -> None:
        self._finalize_chat_context(self._chat_context(
            group["user"], group["content"], group["members"], group["count"],
            group["ts"], group.get("test", False)))

    def _fallback_text(self) -> str | None:
        """兜底口播文案；`safety.fallback_enabled` 关闭时返回 None（用户要求异常时闭嘴）。"""
        safety_cfg = (self._cfg.get("safety", {}) or {})
        if not safety_cfg.get("fallback_enabled", True):
            return None
        return str(safety_cfg.get("fallback_reply", "哈哈这个咱们直播间聊哈"))

    def _fallback_reply(self, context: dict) -> None:
        """兜底口播（safety.fallback_reply）：生成器/智能体异常时的末级容错。

        优先级固定 P1 且不插队——兜底文案是"接住话头"的填充，不是礼物级回应。
        `safety.fallback_enabled` 关闭则静默不产出。
        """
        text = self._fallback_text()
        if text is None:
            self._inc("chat_skipped_none")  # 兜底口播关闭：异常静默
            return
        self._emit_agent_result(context, {"text": text, "priority": 1})

    def _finalize_chat_context(self, context: dict) -> None:
        if self._reply_builder is None:
            # LLM 未配置：弹幕照收照上屏，但不产出回复任务——
            # 占位文本若照常投递会被 TTS 真的说出口（用户需求：未配置也可正常开播）
            self._inc("chat_no_llm")
            self._log.info("chat_skipped_no_llm",
                           {"user": context["user"], "count": context["merged_count"]})
            return
        try:
            result = self._reply_builder(context)
        except Exception as e:  # 生成器自身的异常兜底（正常路径在 generator 内消化）
            self._log.warning("chat_reply_builder_error", {"error": f"{type(e).__name__}: {e}"})
            self._fallback_reply(context)
            return
        if inspect.isawaitable(result):
            # 异步生成器（线上 Generator）：调度后台补齐，不挡高优先级事件
            self._chat_generating += 1
            self._schedule(self._finalize_chat_await(result, context))
        else:
            # 同步生成器（测试/模板）：内联产出，纯同步调用方无需事件循环
            self._emit_agent_result(context, result)

    async def _finalize_chat_await(self, awaitable, context: dict,
                                   count_pending: bool = True) -> None:
        """异步应答补齐。

        默认参数下与 rules 链路的既有行为一字不变（日志名
        `chat_reply_builder_error`、`_chat_generating` 计数）；agent 模式下弹幕以外的
        类型由 `_dispatch_context` 传 `count_pending=False`。

        失败一律落成 `safety.fallback_reply` 兜底口播，且与同步路径共用
        `_fallback_text()`——两条路径的容错不分叉（HANDOVER §6.4 第 37 条）。
        """
        try:
            try:
                result = await awaitable
            except Exception as e:
                self._log.warning("chat_reply_builder_error", {"error": f"{type(e).__name__}: {e}"})
                result = self._fallback_text()
            self._emit_agent_result(context, result)
        finally:
            if count_pending:
                self._chat_generating -= 1

    def has_pending_reply(self) -> bool:
        """是否有弹幕回复在处理中（合并窗口未关闭 / LLM 生成中）——供话题调度让位。"""
        return self._chat_generating > 0 or self._window is not None

    def _emit_agent_result(self, context: dict, result) -> None:
        """应答结果落地（五种事件类型共用，M4 起统一出口）。

        result 三种形态：

        - `str` → 纯文本任务（用该类型的时间戳 / 来源 / 默认优先级）；
        - `dict` → 可带 `audio_path`（v3.1 模板方案的预录音频）、`emotion` / `speed`
          （v4 情绪覆盖）、`priority` / `interrupt`（M4 智能体决策）；
        - `None` → 本次不产出任务（记日志不投递）。

        优先级与插队：以决策给的为准，缺省用该类型的规则档位（礼物 P0、其余 P1）；
        **`interrupt` 仅在 `priority=0` 时生效**（DESIGN §5.3）。
        """
        kind = str(context.get("type") or "chat")
        skip_none = "chat_skipped_none" if kind == "chat" else f"{kind}_skipped_none"
        skip_empty = "chat_skipped_empty" if kind == "chat" else f"{kind}_skipped_empty"

        if result is None:
            self._inc(skip_none)
            self._log.info(skip_none, {"user": context["user"],
                                       "count": context.get("merged_count", 1)})
            return
        audio_path = ""
        emotion, speed = "", None
        priority = _AGENT_DEFAULT_PRIORITY.get(kind, 1)
        interrupt = False
        if isinstance(result, dict):
            text = str(result.get("text") or "").strip()
            audio_path = str(result.get("audio_path") or "").strip()
            # v4 情绪覆盖（DESIGN §8）：应答方可带上情绪与语速；不带时保持空值，
            # 播放侧落回 tts 构造期参数，旧行为一字不变
            emotion = str(result.get("emotion") or "").strip()
            raw_speed = result.get("speed")
            if raw_speed is not None:
                try:
                    speed = float(raw_speed)
                except (TypeError, ValueError):
                    speed = None
            raw_priority = result.get("priority")
            if raw_priority is not None:
                try:
                    priority = max(0, min(2, int(raw_priority)))  # 0 / 1 / 2
                except (TypeError, ValueError):
                    pass
            interrupt = bool(result.get("interrupt"))
        else:
            text = str(result)
        if not text and not audio_path:
            self._inc(skip_empty)
            self._log.info(skip_empty, {"user": context["user"]})
            return
        if priority != 0:
            interrupt = False
        task = SpeakTask(
            priority=priority, text=text, event_ts=context["event_ts"],
            source=kind, merged_count=int(context.get("merged_count") or 1),
            tier=str(context.get("tier") or ""), interrupt=interrupt,
            audio_path=audio_path, emotion=emotion, speed=speed,
            test=bool(context.get("test", False)),
            feedback=self._feedback_context(context, result, text),
        )
        self._emit_task(task, interrupt=interrupt)

    def _feedback_context(self, context: dict, result, text: str) -> dict | None:
        """反馈上下文（DESIGN §7.2③，M6）：**只有智能体决策**才给。

        判定用"结果里有没有 `reason`"这一个条件，正好把三条链路分开：

        | 链路 | 结果形态 | 有 reason？ | 挂按钮？ |
        |---|---|---|---|
        | 智能体 | dict（九字段契约） | 有 | **挂** |
        | v3 模板方案 | dict（text / audio_path / priority） | 无 | 不挂 |
        | 规则/兜底 | str（`_fallback_reply` 的 dict 也无 reason） | 无 | 不挂 |

        于是「开关 OFF 时行为与旧版逐字一致」这条不变式天然成立——不需要再为
        反馈按钮加一层开关判断。`agent.feedback.enabled` 关掉时同样不给（厂长
        级开关，配置里默认 true）。

        卡片只带**这一条回复**所需的最小上下文（事件 + 判断依据 + 回复原文），
        前端原样回传给 `FeedbackStore`，不让页面自己拼字段。
        """
        if not isinstance(result, dict):
            return None
        reason = str(result.get("reason") or "").strip()
        if not reason:
            return None
        try:
            enabled = bool(agent_settings(self._cfg)["feedback"]["enabled"])
        except Exception:      # 配置畸形时宁可不挂按钮，也不影响这条回复播出
            enabled = True
        if not enabled:
            return None
        kind = str(context.get("type") or "chat")
        return {
            "event_type": kind,
            "user": str(context.get("user") or ""),
            "content": str(context.get("content") or ""),
            # 礼物事件的 content 为空，靠这两个字段在样本库里落成「火箭 ×3」
            "gift_name": str(context.get("gift_name") or ""),
            "gift_count": int(context.get("gift_count") or 1),
            "tone": str(result.get("tone") or ""),
            "reason": reason,
            "reply": text,
        }

    # ---------- gift：连击状态机（PRD FR-2，DESIGN §6.2） ----------

    def _handle_gift(self, ev: LiveEvent, now: float) -> None:
        if not self._gift_enabled:
            return
        if self._mode == MODE_AGENT:
            # agent 模式：跳过连击状态机，逐条礼物事件直接产出（不做合并/超时结算）
            self._emit_gift({
                "user": ev.user, "gift_name": ev.gift_name,
                "count": ev.gift_count, "value": ev.gift_value,
                "test": bool(ev.test), "ts": ev.ts,
            }, now)
            return
        key = (ev.user, ev.gift_name)
        state = self._combos.get(key)
        if state is None:
            state = {"user": ev.user, "gift_name": ev.gift_name,
                     "count": 0, "value": 0, "last_ts": now,
                     "test": bool(ev.test)}
            self._combos[key] = state
        # TODO(OQ-2): 连击中间消息的 count 语义（累计/增量）待步骤 2.4 真实数据核对；
        # 当前按累计口径取 max，若实测为增量需改为累加（换算集中此一处）。
        state["count"] = max(state["count"], ev.gift_count)
        state["value"] = max(state["value"], ev.gift_value)
        state["last_ts"] = now
        if ev.combo_end:
            self._settle_gift(key, now)

    def _settle_due_gifts(self, now: float) -> None:
        for key in list(self._combos.keys()):
            state = self._combos[key]
            if now - state["last_ts"] > self._combo_timeout_s:
                self._settle_gift(key, now)

    def _settle_gift(self, key, now: float) -> None:
        state = self._combos.pop(key, None)
        if state is None:
            return
        self._emit_gift(state, now)

    def _emit_gift(self, state: dict, now: float) -> None:
        """礼物感谢落地：分档 → 逐档开关 → （智能体决策 | 模板渲染）→ 产出。

        state 需含 user / gift_name / count / value / test；rules 模式由连击结算
        传入累计值，agent 模式由单条礼物事件直接构造（M3）。

        M4 起：`_agent_active()` 时把事件上下文交给智能体（DESIGN §5.1「是否感谢、
        怎么感谢交智能体」），优先级与插队由决策给出，不再按档位硬定；
        其余情况保持规则模式原文——分档、逐档开关、模板渲染、P0 与 mid/high 插队。
        """
        value = state["value"]
        tier = gift_tier(value)
        interrupt = tier in ("mid", "high")
        # 事件模板库逐档开关：关闭则跳过该档礼物感谢（测试事件绕过）
        if not self._event_enabled.get(f"gift_{tier}", True) and not state.get("test"):
            self._inc("gift_disabled")
            self._log.info("gift_disabled", {"tier": tier, "user": state["user"]})
            return
        if self._agent_active():
            self._dispatch_context(self._gift_context(state, now, tier))
            return
        fields = {"user": state["user"], "gift": state["gift_name"],
                  "count": state["count"], "tier": tier, "value": value}
        if self._template_renderer is not None:
            text = str(self._template_renderer("gift", fields))
        else:
            text = _GIFT_TEXT_PLACEHOLDER.format(**fields)
        task = SpeakTask(
            priority=0, text=text, event_ts=now, source="gift",
            tier=tier, interrupt=interrupt,
            test=bool(state.get("test", False)),
        )
        self._log.info("gift_settled", {"user": state["user"], "gift": state["gift_name"],
                                        "count": state["count"], "value": value, "tier": tier})
        self._emit_task(task, interrupt=interrupt)

    # ---------- follow / fansclub：冷却加概率（PRD FR-3） ----------

    def _handle_follow(self, ev: LiveEvent, now: float) -> None:
        if ev.test:
            self._emit_cooldown_task("follow", ev.user, now, force=True)
            return
        self._handle_cooldown_kind("follow", ev, now)

    def _handle_fansclub(self, ev: LiveEvent, now: float) -> None:
        content = (ev.content or "").strip()
        if content and self._input_filter(content):
            self._inc("blocked_input")
            self._log.info("fansclub_blocked_input", {"user": ev.user, "content": content})
            return
        if ev.test:
            self._emit_cooldown_task("fansclub", ev.user, now, force=True)
            return
        self._handle_cooldown_kind("fansclub", ev, now)

    def _handle_cooldown_kind(self, kind: str, ev: LiveEvent, now: float) -> None:
        cfg = self._cooldown_cfg[kind]
        if not cfg["enabled"]:
            return
        # 事件模板库逐类开关：关闭则跳过（测试事件绕过）
        if not self._event_enabled.get(kind, True) and not ev.test:
            self._inc(f"{kind}_disabled")
            self._log.info(f"{kind}_disabled", {"user": ev.user})
            return
        if self._agent_active():
            # M4：智能体接管——谢不谢、怎么谢（DESIGN §5.1）交给它判断
            self._dispatch_context(self._event_context(ev, now))
            return
        if self._mode == MODE_AGENT:
            # agent 模式但智能体尚未接管（M3 降级路径）：跳过冷却与概率判断，
            # 事件来一条算一条，文本仍走本地模板
            self._emit_cooldown_task(kind, ev.user, now, bypass=True)
            return
        cd = self._cooldowns[kind]
        if now < cd["until"]:
            cd["pending_count"] += 1
            cd["pending_user"] = ev.user
            self._inc(f"{kind}_suppressed")
            self._log.debug(f"{kind}_cooldown_suppressed", {"user": ev.user})
            return
        self._emit_cooldown_task(kind, ev.user, now)

    def _flush_cooldown_pending(self, now: float) -> None:
        """冷却期结束：若期间有积压，取最近一位产出任务（PROMPTS 步骤 2.3）。"""
        for kind, cd in self._cooldowns.items():
            if cd["pending_count"] > 0 and now >= cd["until"]:
                user = cd["pending_user"]
                cd["pending_count"] = 0
                cd["pending_user"] = ""
                self._emit_cooldown_task(kind, user, now)

    def _emit_cooldown_task(self, kind: str, user: str, now: float,
                            force: bool = False, bypass: bool = False) -> None:
        """产出欢迎任务。force=测试事件（不占冷却、标记 test）；
        bypass=agent 模式（不占冷却、不掷概率，但仍是真实任务，test 保持 False）。"""
        cd = self._cooldowns[kind]
        if not force and not bypass:
            cd["until"] = now + self._cooldown_cfg[kind]["cooldown_s"]
            if self._random.random() >= self._cooldown_cfg[kind]["probability"]:
                self._inc(f"{kind}_skip_prob")
                self._log.debug(f"{kind}_skip_prob", {"user": user})
                return
        fields = {"user": user, "kind": kind}
        if self._template_renderer is not None:
            text = str(self._template_renderer(kind, fields))
        else:
            text = _WELCOME_TEXT_PLACEHOLDER.format(user=user)
        task = SpeakTask(priority=1, text=text, event_ts=now, source=kind, test=force)
        self._log.info(f"{kind}_task_emitted", {"user": user})
        self._emit_task(task, interrupt=False)

    # ---------- enter：默认关闭（DESIGN §4.2） ----------

    def _handle_enter(self, ev: LiveEvent, now: float) -> None:
        # 存档事件开关优先；无存档/未注入时回退 config events.enter.enabled
        if not self._event_enabled.get("enter", self._enter_enabled) and not ev.test:
            self._inc("enter_ignored")
            return
        if self._agent_active():
            # M4：进场欢迎也交给智能体（欢迎谁、热不热情由它定）
            self._dispatch_context(self._event_context(ev, now))
            return
        fields = {"user": ev.user}
        text = (str(self._template_renderer("enter", fields))
                if self._template_renderer is not None
                else _WELCOME_TEXT_PLACEHOLDER.format(user=ev.user))
        self._emit_task(SpeakTask(priority=1, text=text, event_ts=now, source="enter",
                                  test=bool(ev.test)),
                        interrupt=False)

    # ---------- 任务投递 ----------

    def _emit_task(self, task: SpeakTask, interrupt: bool) -> None:
        task.interrupt = task.interrupt or interrupt
        self._inc("task_produced")
        result = self._on_task(task)
        if inspect.isawaitable(result):
            self._schedule(result)

    def _schedule(self, coro) -> None:
        """在途异步工作登记进 _inflight（无事件循环的纯同步单测不应走到这里）。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError("异步产出需要运行中的事件循环（on_task/reply_builder 为异步时）")
        self._inflight.append(asyncio.ensure_future(coro))

    def _inc(self, name: str, n: int = 1) -> None:
        if self._stats is not None:
            self._stats.inc(name, n)
