"""智能体决策主流程（DESIGN §4.2 / §5.3）：Router 的接管入口。

一次决策的完整链路：

```
事件上下文 → ① 写入单场记忆
           → ② L2 召回（存档素材 + 反馈样本，前者由 build_system_prompt 直注 L1、
                后者仅作为候选参与打分；样本带 kind=example，M6-2）
           → ③ 组装 system prompt / user message
           → ④ LLM 一次调用（复用 config.llm）
           → ⑤ decide 解析契约（四级容错）
           → ⑥ 情绪与语速校验回落
           → ⑦ 记忆回写（说过的 / 笔记）+ 召回去重标记
           → ⑧ 决策日志落盘
           → 返回 {text, audio_path, emotion, speed, priority, interrupt, tone, reason} | None
```

**返回契约**（与 `Router` 的 `reply_builder` 同族，多几个字段供钩子推广消费）：

| 键 | 说明 |
|---|---|
| `text` | 要说的口播文本 |
| `audio_path` | 命中了预录素材时有值；空串 → 播放侧现场 TTS |
| `emotion` / `speed` | 已校验落地的情绪与语速（`speed=None` = 不覆盖音色档案参数） |
| `priority` / `interrupt` | 决策建议的优先级与是否插队 |
| `tone` / `reason` | 语气与判断依据（进决策日志，M5 决策透视展示） |

返回 `None` = **这次不回**（`should_reply=false` 或文本为空），调用方不产出任务（V4.3）。

**算与写分开**：`_evaluate()` 只算（组 prompt → 调模型 → 解析 → 校验 → 召回），
`_decide()` 在它之上把单场记忆、召回去重窗口、决策日志写实，`preview()` 则**只算不写**——
M5 试玩调试靠这条分支做到"能看完整决策，但直播链路零影响"（V5.3）。

**预录音频复用**：智能体若原样采纳了某条召回素材的话术，就去解析**那条素材**的预录
音频（`selector.resolve_audio`）。不这么做的话，agent 模式下每条回复都是现场合成的
新文本，「情绪档位 / 预录音频」这套机制在开启智能体后等于废掉，prompt 里给的
"有预录音频可立即出声"标注也成了空话。

**异常策略**：LLM 连续失败（`AgentLLMError`）与输出不可用（`DecisionError`）都**向上抛**，
由 Router 的末级容错统一落成 `safety.fallback_reply` 兜底口播——兜底只此一处，
本模块不重复实现（DESIGN §5.3 第 3 级、HANDOVER §6.4 第 37 条）。
"""
import time

from hub.agent.decide import parse_decision
from hub.agent.emotion import resolve_emotion, resolve_speed
from hub.agent.llm import AgentLLM
from hub.agent.log import DecisionLogger
from hub.agent.memory import SessionMemory
from hub.agent.prompt import build_agent_system_prompt, build_agent_user_message, describe_event
from hub.agent.retriever import L2Retriever
from hub.config import agent_settings
from hub.events import LiveEvent
from hub.logger import get_logger
from hub.prompt import build_system_prompt

# 判定"智能体原样采纳了这条召回素材"的相似度阈值（低于它就走现场合成）
AUDIO_REUSE_SIM = 0.9
# 决策日志里的召回 id 最多记几条（够 M5 决策透视展示即可）
MAX_LOGGED_RECALLS = 8


def _similarity(a: str, b: str) -> float:
    from hub.router import default_similarity
    return default_similarity(a, b)


class AgentBrain:
    """智能体决策主体。构造一次、长期复用；每场直播调 `reset_session()` 清记忆。"""

    def __init__(self, config: dict = None, settings: dict = None, selector=None,
                 memory: SessionMemory = None, retriever=None, llm=None,
                 decision_log: DecisionLogger = None, examples=None,
                 examples_provider=None,
                 stats=None, log=None, clock=None, log_dir=None):
        self._cfg = config or {}
        self._settings = (agent_settings(self._cfg) if settings is None
                          else dict(settings))
        self._selector = selector
        self._stats = stats
        self._log = log or get_logger("agent.brain")
        self._clock = clock or time.time
        self._examples = list(examples or [])
        # 反馈样本的**现读**通道（M6-2）：传 callable 则每次决策现取，
        # 换"主播点完采纳，下一条立刻用上新样本"；传 list 只在构造期固定（测试/脚本用）。
        self._examples_provider = examples_provider

        mem_cfg = self._settings.get("memory") or {}
        self._memory = memory or SessionMemory(
            max_events=int(mem_cfg.get("max_events", 50)),
            max_spoken=int(mem_cfg.get("max_spoken", 20)),
            max_notes=int(mem_cfg.get("max_notes", 10)),
            log=self._log)

        audio_resolver = getattr(selector, "resolve_audio", None) if selector else None
        self._retriever = retriever or L2Retriever(
            top_k=int((self._settings.get("retrieve") or {}).get("top_k", 5)),
            audio_resolver=audio_resolver, log=self._log)

        self._llm = llm or AgentLLM(config=self._cfg, stats=stats, log=self._log,
                                    clock=self._clock)
        self._decision_log = decision_log if decision_log is not None else DecisionLogger(
            log_dir=log_dir, enabled=bool(self._settings.get("decision_log", True)),
            clock=self._clock, log=self._log)

    # ---------- 对外 ----------

    def handle(self, context: dict):
        """Router 接管入口：同步调用、返回 awaitable（与 `reply_builder` 同契约）。"""
        return self._decide(context)

    async def preview(self, context: dict) -> dict:
        """试玩调试入口（DESIGN §7.2②）：算一条完整决策，**不碰任何直播状态**（V5.3）。

        与 `_decide` 的差别只有"写不写"三处：单场记忆、召回去重窗口、决策日志。
        读的仍是**当前**的人设素材与单场记忆——试玩要看的正是"此刻这条事件它会怎么回"，
        把记忆读成空的就没意义了（所以"隔离"= 不写，不是不读）。

        返回**决策日志同形状**的记录（含 `should_reply` / `event_line` / `reason` /
        `recall` / `degrade` / `latency_ms`），页面直接渲染即可，不用二次拼装。
        """
        ev = await self._evaluate(dict(context or {}))
        record = self._record(ev)
        record["should_reply"] = bool(ev["decision"].should_reply)
        return record

    @property
    def memory(self) -> SessionMemory:
        return self._memory

    @property
    def retriever(self):
        return self._retriever

    @property
    def decision_log(self) -> DecisionLogger:
        return self._decision_log

    @property
    def model(self) -> str:
        return self._llm.model

    def reset_session(self) -> None:
        """开播 / 换场次：清空单场记忆与召回去重窗口（配置不改）。"""
        self._memory.clear()
        self._retriever.clear_recent()

    def rebind_llm(self, config: dict = None) -> None:
        """LLM 热配置变更后重建传输层（控制台保存 LLM 配置后调用）。

        只换 LLM 客户端：单场记忆、召回去重窗口、决策日志都保持不动——
        它们是"这一场直播的上下文"，不该因为主播改了个 Key 就清零。
        """
        if config is not None:
            self._cfg = config
        self._llm = AgentLLM(config=self._cfg, stats=self._stats, log=self._log,
                             clock=self._clock)
        self._log.info("agent_llm_rebound", {"model": self._llm.model})

    # ---------- 主流程 ----------

    async def _evaluate(self, ctx: dict) -> dict:
        """算一条决策的**全部中间产物**，且**不做任何写操作**。

        拆出来的理由有两条：
        - 试玩调试（DESIGN §7.2②）要的是"完整决策"，但不能碰直播状态（V5.3）——
          于是把"算"与"写"分开：本方法只管算，记忆回写 / 日志落盘留给调用方；
        - 决策日志的记录形状与试玩返回给页面的形状本就该是同一份，共用组装逻辑
          （见 `_record`）才不会出现"日志里有 emotion_raw、页面上没有"这类偏差。
        """
        event = self._to_event(ctx)

        # ② L2 召回（无查询文本的事件跳过，省一次打分）
        query = self._query_text(event)
        recalls = []
        if query:
            self._retriever.index(self._selector_profile(), self._examples_now())
            recalls = self._retriever.retrieve(query)

        # ③④⑤ 组 prompt → 调模型 → 解析契约
        started = self._clock()
        raw = await self._llm.ask(self._build_system_prompt(recalls), self._build_user_message(ctx))
        decision = parse_decision(raw, self._resolve_max_reply_chars())
        latency_ms = int(round((self._clock() - started) * 1000))

        # ⑥ 情绪与语速校验回落
        emotion, emotion_event = resolve_emotion(
            decision.emotion, self._emotions(),
            enabled=bool((self._settings.get("emotion") or {}).get("enabled", True)),
            onsite_fallback=bool((self._settings.get("emotion") or {}).get(
                "onsite_fallback", True)))
        speed, speed_event = resolve_speed(decision.speed)

        # 预录音频复用：只读 selector，无副作用
        audio_path = (self._reuse_prerecorded(decision.reply_text, recalls, emotion)
                      if decision.has_reply() else "")

        return {"ctx": ctx, "event": event, "decision": decision, "recalls": recalls,
                "latency_ms": latency_ms, "emotion": emotion,
                "emotion_event": emotion_event, "speed": speed,
                "speed_event": speed_event, "audio_path": audio_path}

    async def _decide(self, context: dict) -> dict | None:
        """直播链路：算一条决策 + 把记忆与日志写实，返回下游可消费的 dict。"""
        ctx = dict(context or {})
        # ① 记忆先落地：should_reply=false 的事件也要记（否则看不出"这条刚被问过"）
        self._memory.add_event(self._to_event(ctx))

        ev = await self._evaluate(ctx)
        decision, event = ev["decision"], ev["event"]

        # ⑦ 记忆回写 + 召回去重
        if decision.has_reply():
            self._memory.add_spoken(decision.reply_text)
            self._retriever.note_spoken(decision.reply_text)
        self._memory.add_note(decision.memory_note)

        # ⑧ 决策日志
        self._write_log(ev)

        result = self._to_result(ev)
        if result is None:
            self._inc("agent_no_reply")
            self._log.info("agent_decision_no_reply", {
                "type": event.type, "user": event.user,
                "reason": decision.reason, "degrade": decision.degrade})
            return None

        self._inc("agent_replied")
        if ev["emotion_event"]:
            self._inc(ev["emotion_event"])
        self._log.info("agent_decided", {
            "type": event.type, "user": event.user, "priority": decision.priority,
            "interrupt": decision.interrupt, "emotion": ev["emotion"],
            "emotion_event": ev["emotion_event"], "latency_ms": ev["latency_ms"],
            "prerecorded": bool(ev["audio_path"]), "degrade": decision.degrade,
            "reply_chars": len(decision.reply_text)})
        return result

    # ---------- 结果组装 ----------

    @staticmethod
    def _to_result(ev: dict) -> dict | None:
        """下游（Router）消费的结果：`None` = 这次不回。"""
        decision = ev["decision"]
        if not decision.has_reply():
            return None
        return {
            "text": decision.reply_text,
            "audio_path": ev["audio_path"],
            "emotion": ev["emotion"],
            "speed": ev["speed"],
            "priority": decision.priority,
            "interrupt": decision.interrupt,
            "tone": decision.tone,
            "reason": decision.reason,
        }

    # ---------- 组装 ----------

    def _build_system_prompt(self, recalls) -> str:
        return build_agent_system_prompt(
            self._base_prompt(), recalls=recalls, memory_brief=self._memory.brief(),
            emotions=self._emotions(),
            onsite_fallback=bool((self._settings.get("emotion") or {}).get(
                "onsite_fallback", True)),
            max_reply_chars=self._resolve_max_reply_chars())

    @staticmethod
    def _build_user_message(context: dict) -> str:
        return build_agent_user_message(context)

    def _base_prompt(self) -> str:
        """L1 素材直注：存档人设 + 知识库 + 话题话风 + 样本示例（没存档则回落 config）。"""
        if self._selector is not None:
            return self._selector.build_system_prompt(self._cfg)
        return build_system_prompt(self._cfg)

    def _emotions(self) -> list:
        if self._selector is not None:
            try:
                return list(self._selector.current_emotions())
            except Exception:  # 存档读到脏数据也不能挡住决策
                return []
        return []

    def _selector_profile(self) -> dict | None:
        return self._selector.profile if self._selector is not None else None

    def _examples_now(self) -> list:
        """反馈样本**每次决策现取**（M6-2）。

        样本库是主播边播边攒的：点完采纳，下一条决策就该用上，所以不做缓存、
        不做失效逻辑（`FeedbackStore` 现读几 KB 的 jsonl，<1 ms）。
        读失败按"这次没有可用样本"处理——样本是加分项，不能因为文件坏了挡住回复。

        召回素材里的 `example:*` 条目天然没有预录音频（`_resolve_audio` 查
        `audios` 键得空 → 返回空串），所以命中示例只会走现场合成，不会误播别条音频。
        """
        if self._examples_provider is None:
            return self._examples
        try:
            return list(self._examples_provider() or [])
        except Exception as e:
            self._log.warning("agent_examples_read_failed", {
                "error": f"{type(e).__name__}: {e}"})
            return []

    def _resolve_max_reply_chars(self) -> int:
        """回复字数上限**每次决策现读**，不在构造期锁定。

        两个理由：① 装配时机——main 里 brain 可能建在存档装载完成之前，
        构造期读会锁到 config 兜底值；② 热生效——主播在话术管理页改「回复字数上限」
        或换存档后，下一次决策就该按新值走。
        """
        if self._selector is not None:
            try:
                return int(self._selector.max_reply_chars())
            except Exception:
                pass
        return int(((self._cfg.get("persona") or {}).get("max_reply_chars", 40)))

    # ---------- 预录音频复用 ----------

    def _reuse_prerecorded(self, reply_text: str, recalls, emotion: str) -> str:
        """回复若与某条召回素材高度一致，播它的预录音频（省一次合成与 2~4 s 延迟）。"""
        if self._selector is None or not recalls:
            return ""
        best, best_score = None, 0.0
        for r in recalls:
            candidate = str(r.get("reply") or "")
            if not candidate:
                continue
            try:
                score = _similarity(candidate, reply_text)
            except Exception:
                continue
            if score > best_score:
                best, best_score = r, score
        if best is None or best_score < AUDIO_REUSE_SIM:
            return ""
        item = self._retriever.entry_item(best["id"])
        if not item:
            return ""
        try:
            return str(self._selector.resolve_audio(item, emotion) or "")
        except Exception as e:
            self._log.warning("agent_audio_reuse_failed", {
                "entry": best["id"], "error": f"{type(e).__name__}: {e}"})
            return ""

    # ---------- 辅助 ----------

    @staticmethod
    def _query_text(event: LiveEvent) -> str:
        """召回查询文本：弹幕/粉丝团附言用正文，礼物用礼物名，其余不召回。"""
        if event.type in ("chat", "fansclub"):
            return str(event.content or "").strip()
        if event.type == "gift":
            return str(event.gift_name or "").strip()
        return ""

    @staticmethod
    def _to_event(ctx: dict) -> LiveEvent:
        return LiveEvent(
            type=str(ctx.get("type") or "chat"),
            user=str(ctx.get("user") or ""),
            content=str(ctx.get("content") or ""),
            gift_name=str(ctx.get("gift_name") or ""),
            gift_count=int(ctx.get("gift_count") or 1),
            gift_value=int(ctx.get("gift_value") or 0),
            ts=float(ctx.get("event_ts") or 0.0),
            test=bool(ctx.get("test", False)),
        )

    def _write_log(self, ev: dict) -> None:
        if not self._decision_log.enabled:
            return
        self._decision_log.write(self._record(ev))

    def _record(self, ev: dict) -> dict:
        """决策日志的一条记录（试玩页返回给页面的也是这个形状）。"""
        decision = ev["decision"]
        event = ev["event"]
        base = decision.to_record()
        return {
            "type": event.type,
            "user": event.user,
            "event_line": describe_event(ev["ctx"]),
            "tier": str(ev["ctx"].get("tier") or ""),
            "should_reply": base["should_reply"],
            "priority": base["priority"],
            "interrupt": base["interrupt"],
            "tone": base["tone"],
            "reason": base["reason"],
            # 模型给的情绪与最终落地的情绪都要留：否则事后分不清
            # "模型选歪了" 和 "被我们的回落规则改掉了"
            "emotion_raw": base["emotion"],
            "emotion": ev["emotion"],
            "emotion_event": ev["emotion_event"],
            "speed": ev["speed"],
            "speed_event": ev["speed_event"],
            "reply_text": base["reply_text"],
            "memory_note": base["memory_note"],
            "degrade": base["degrade"],
            "latency_ms": ev["latency_ms"],
            "model": self._llm.model,
            "recall": [r["id"] for r in list(ev["recalls"] or [])[:MAX_LOGGED_RECALLS]],
            "prerecorded": bool(ev["audio_path"]),
            "test": bool(ev["ctx"].get("test", False)),
        }

    def _inc(self, name: str) -> None:
        if self._stats is not None:
            self._stats.inc(name)
