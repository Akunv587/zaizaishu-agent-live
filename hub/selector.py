"""话术运行时（话术管理 v4：单方案纯AI + 存档内多音色）。

弹幕应答（纯AI，三级）：
① 关键词直出——弹幕命中样本 examples/type_name（互含或相似）直接播该样本，
  不走 LLM（有当前音色音频播音频；无音频/音频过期则现场 TTS 念当前文本）；
  剔除最近播过的（防复读）；
② LLM 选择（兜底）——①未命中时从「有当前音色音频」的样本库选一条最合适的回复；
③ 现场生成——无候选 / 未选中 / 选择失败 → Generator 现场生成智能回复
  （system 提示词 = 知识库 + 话题话风参考 + AI 样本示例）。永不静默
  （除非未配置 LLM）。话题仅作话风参考与轮播素材，不参与回复选择。

多音色：存档 voices + current_voice 决定发声音色——话题轮播只播当前音色的
预录音频（缺音频/文本已改过期的条目跳过并记日志）；弹幕回复选中的样本播当前
音色音频，过期音频视为无音频（回退现场 TTS 念新文本）；实时 TTS 由 main 的
_apply_live_voice 按存档当前音色+参数热切换（config.yaml 的 tts.voice_id 仅作
启动兜底，页面不再写它）。

多情绪（v4，DESIGN §8.3）：音频按「音色 × 情绪档」存放，`_resolve_audio(item,
voice_id, emotion)` 做三级回落——请求档命中则播预录；档位**已登记**但缺音频则
回落 `default` 预录（记 sc_emotion_fallback）；档位**未登记**则返回空串，由播放侧
现场 TTS 并带上该情绪。音色不按句切换，只有情绪/语速随任务覆盖。

存档供料：事件模板（礼物三档/欢迎三组）覆盖给 TemplateEngine；启用话题列表
给 Scheduler 做秒级轮播（只供有当前音色音频的条目）；知识库 + AI 样本给
Generator 组 system 提示词。
reload() 全量重读指针与存档——控制台保存/切换存档/切换音色后调用一次即热生效。
LLM 选择器经 set_llm_selector 注入（Generator.select_reply），随 LLM 配置热替换；
现场生成器经 set_fallback_replier 注入（Generator.generate_reply，可被包装）。
"""
import re
import time
from pathlib import Path

from hub.config import script_settings
from hub.logger import get_logger
from hub.prompt import build_system_prompt
from hub.router import default_similarity
from hub.scriptstore import (
    EMOTION_DEFAULT,
    ScriptStoreError,
    normalize_emotion,
    normalize_emotions,
    text_hash,
)
from hub.templates import _count_text, _shorten_name

# 组提示词时最多带多少条样本示例（防样本过多撑爆上下文）
MAX_FEWSHOT_SAMPLES = 20
# 喂给 LLM 的选择候选上限（防选择提示词过长）
MAX_SELECT_CANDIDATES = 30
# 防复读：最近播过的候选在选择时剔除（秒）。300s 在口播密集时仍会让
# 同一话题 5 分钟内回归候选，拉长到 600s 进一步稀释复播感。
RECENT_PICK_WINDOW_S = 600
# 话题文本作为话风参考注入提示词时最多取几条（防话题过长撑爆上下文）
MAX_TOPIC_STYLE_REFS = 5
# 单条话题话风参考的最大字符数（话题一般较长，取前段即可体现语气与用词）
TOPIC_STYLE_REF_CHARS = 80
# 关键词直出：弹幕与样本 examples/type_name 的相似度阈值（≥该值直接命中，不走 LLM）
KEYWORD_SIM_THRESHOLD = 0.6

_SYMBOL_RE = re.compile(r"[\W_]+", re.UNICODE)


def _norm_keyword(text: str) -> str:
    """归一化：去空白/标点/emoji，转小写——供关键词直出的互含与相似匹配。"""
    return _SYMBOL_RE.sub("", str(text or "")).lower()


def fill_reply_fields(template: str, fields: dict, prefix_len: int) -> str:
    """替换回复模板里的动态字段（{user}/{gift}/{count}），规则同 TemplateEngine。"""
    fields = fields or {}
    values = {
        "user": _shorten_name(str(fields.get("user", "")), prefix_len),
        "gift": str(fields.get("gift", "")),
        "count": _count_text(fields.get("count", 1)),
    }
    try:
        return str(template or "").format(**values)
    except (KeyError, IndexError):
        return str(template or "")


class ScriptRuntime:
    """激活存档的运行时视图（发声音色=存档 current_voice）。构造后必须 reload() 一次。"""

    def __init__(self, store, config: dict = None, stats=None, log=None):
        self._store = store
        self._cfg = config or {}
        self._stats = stats
        self._log = log or get_logger("selector")
        self._llm_selector = None       # async (danmaku_text, candidates) -> id | None
        self._fallback_replier = None   # async (context) -> str：未命中时的现场生成
        self._no_llm_warned = False
        self._recent: dict[str, float] = {}   # 最近选播的候选 id → monotonic 时间
        self._profile_id = ""
        self._profile: dict | None = None
        self._load_error = ""

    def set_llm_selector(self, fn) -> None:
        """注入/热替换 LLM 选择器（Generator.select_reply）。

        None 表示未配置 LLM：LLM 选择级停用，弹幕应答直接走现场生成降级链。
        """
        self._llm_selector = fn
        self._no_llm_warned = False
        self._log.info("llm_selector_swapped", {"active": fn is not None})

    def set_fallback_replier(self, fn) -> None:
        """注入/热替换未命中候选时的现场生成器（Generator.generate_reply）。

        None 表示未配置 LLM：弹幕应答停用（调用方据此不挂路由）。
        """
        self._fallback_replier = fn
        self._recent.clear()
        self._log.info("fallback_replier_swapped", {"active": fn is not None})

    # ---------- 装载 ----------

    def reload(self) -> str:
        """重读 script 指针与激活存档；返回需写回 config.yaml 的兜底存档 id（无则空串）。

        指针留空或指向不存在/损坏的存档时，经 ScriptStore.ensure_default 兜底
        并返回新 id 由调用方持久化——下次启动不再重复兜底。
        """
        settings = script_settings(self._cfg)
        pid = settings["active_profile"]
        defaulted = ""
        if pid and not self._store.exists(pid):
            self._log.warning("script_profile_missing", {"id": pid})
            pid = ""
        if not pid:
            pid = self._store.ensure_default()
            defaulted = pid
        try:
            self._profile = self._store.get_profile(pid)
            self._profile_id = pid
            self._load_error = ""
        except ScriptStoreError as e:
            self._profile = None
            self._profile_id = pid
            self._load_error = str(e)
            self._log.error("script_profile_load_failed", {"id": pid, "error": str(e)})
        return defaulted

    # ---------- 状态 ----------

    @property
    def scheme(self) -> str:
        """回复方案恒为 ai（模板方案已退役；字段保留仅为兼容前端快照）。"""
        return "ai"

    @property
    def profile_id(self) -> str:
        return self._profile_id

    @property
    def profile(self) -> dict | None:
        return self._profile

    @property
    def load_error(self) -> str:
        return self._load_error

    def status(self) -> dict:
        """控制台快照用：存档摘要（含当前直播音色）。"""
        p = self._profile or {}
        return {"scheme": "ai", "profile_id": self._profile_id,
                "profile_name": p.get("name", ""),
                "topics_ai": len(p.get("topics_ai") or []),
                "samples_ai": len(p.get("samples_ai") or []),
                "voices": len(p.get("voices") or []),
                "current_voice": p.get("current_voice", ""),
                "error": self._load_error}

    # ---------- 存档音色 ----------

    def current_voice(self) -> str:
        """存档当前直播音色 voice_id（无存档/未设置返回空串）。"""
        p = self._profile
        return str(p.get("current_voice") or "").strip() if p else ""

    def voice_params(self, voice_id: str = "") -> dict:
        """存档内某音色的微调参数（空 dict = 跟随音色工坊档案参数或默认）。"""
        p = self._profile
        if p is None:
            return {}
        vid = str(voice_id or "").strip() or self.current_voice()
        for v in p.get("voices") or []:
            if v.get("voice_id") == vid:
                return dict(v.get("params") or {})
        return {}

    def current_emotions(self) -> list:
        """存档情绪档位集合（default 恒在；无存档返回 []）。"""
        p = self._profile
        return normalize_emotions(p.get("emotions")) if p else []

    def resolve_audio(self, item: dict, emotion: str = "") -> str:
        """公开的音频解析入口（三级回落，DESIGN §8.3）。

        与 `_resolve_audio` 同一实现，仅固定 `voice_id=""`（= 走存档当前音色）——
        音色全场固定是本设计的显式约束（DESIGN §8.1），不需要也不允许按条目切换。
        M4 智能体用它在两处：检索层标注 `has_audio`、以及判定"这条回复能否复用预录
        音频"（智能体若原样复用了某条召回素材，就该播它的预录而不是再合成一遍）。
        """
        return self._resolve_audio(item, "", emotion)

    # ---------- ai 方案：system 提示词 ----------

    def build_system_prompt(self, base_cfg: dict = None) -> str:
        """存档知识库渲染人设提示词；AI 样本库以 few-shot 示例附在尾部。

        存档缺失时回退 base_cfg（config.yaml 的 persona/game 组，与 v3.0 一致）。
        """
        p = self._profile
        if p is None:
            return build_system_prompt(base_cfg or self._cfg)
        k = p["knowledge"]
        cfg_like = {
            "persona": {"nickname": k["nickname"], "style": k["style"],
                        "max_reply_chars": k["max_reply_chars"]},
            "game": {"name": k["game_name"], "intro": k["game_intro"],
                     "download_hint": k["download_hint"],
                     "highlights": k["highlights"]},
        }
        prompt = build_system_prompt(cfg_like)
        # 话题不再作回复候选，仅作为话风/内容参考注入
        topic_refs = []
        for t in p.get("topics_ai") or []:
            if not t.get("enabled"):
                continue
            txt = str(t.get("text") or "").strip()
            if not txt:
                continue
            topic_refs.append(txt[:TOPIC_STYLE_REF_CHARS])
            if len(topic_refs) >= MAX_TOPIC_STYLE_REFS:
                break
        if topic_refs:
            prompt += ("\n直播间话题与话风参考（模仿这些话题的语气、用词与信息量，"
                       "但不要照搬整段）：\n"
                       + "\n".join(f"· {t}" for t in topic_refs))
        shots = []
        for s in p.get("samples_ai") or []:
            if not s.get("enabled") or not s.get("reply_text"):
                continue
            example = (s.get("examples") or [""])[0]
            if example:
                shots.append(f"弹幕「{example}」→ 回复「{s['reply_text']}」")
            if len(shots) >= MAX_FEWSHOT_SAMPLES:
                break
        if shots:
            prompt += "\n常见弹幕回复示例（模仿语气与详略）：\n" + "\n".join(shots)
        return prompt

    def max_reply_chars(self, default: int = 40) -> int:
        p = self._profile
        if p is None:
            return int(((self._cfg.get("persona") or {}).get("max_reply_chars", default)))
        return int(p["knowledge"].get("max_reply_chars", default))

    # ---------- 事件模板覆盖（礼物/欢迎） ----------

    def event_templates(self) -> dict | None:
        """存档的事件模板映射（gift_low/mid/high、follow/fansclub/enter）；无存档返回 None。"""
        p = self._profile
        return dict(p["event_templates"]) if p is not None else None

    def event_enabled(self) -> dict | None:
        """存档的事件开关（gift_low/mid/high、follow/fansclub/enter）；无存档返回 None。"""
        p = self._profile
        return dict(p["event_enabled"]) if p is not None else None

    # ---------- 轮播调度供料 ----------

    def schedule(self) -> dict:
        """秒级轮播配置：enabled/interval_s/jitter_s + 当前音色可播的话题条目。

        只供「有当前直播音色音频且未过期」的话题（缺音频即跳过并记日志——
        轮播统一播预录音频，不再现场 TTS 念话题）。无存档时 enabled=False。
        """
        p = self._profile
        if p is None:
            return {"enabled": False, "interval_s": 30, "jitter_s": 15, "items": []}
        sc = p["schedule"]
        items = []
        skipped = 0
        for t in p.get("topics_ai") or []:
            if not t.get("enabled"):
                continue
            audio = self._resolve_audio(t)
            if not audio:
                skipped += 1
                continue
            items.append({"text": str(t.get("text") or "").strip(),
                          "audio_path": audio})
        if skipped:
            self._log.warning("schedule_topics_no_audio", {
                "skipped": skipped, "voice": self.current_voice(),
                "hint": "话术管理页可一键生成该音色全部音频"})
        return {"enabled": bool(sc.get("enabled", True)),
                "interval_s": int(sc.get("interval_s", 30)),
                "jitter_s": int(sc.get("jitter_s", 15)),
                "items": items}

    # ---------- 弹幕三级应答（关键词直出 → LLM 选择 → 现场生成） ----------

    async def ai_replier(self, context: dict):
        """async (context) -> {"text","audio_path"} | str | None：Router reply_builder 注入点。

        三级应答：
        ① 关键词直出——弹幕命中样本 examples/type_name（互含或相似）直接播该样本，
          不走 LLM（有当前音色音频播音频，无音频/已过期现场 TTS 念当前文本）；
          剔除最近播过的（防复读）；
        ② LLM 选择（兜底）——①未命中时从「有当前音色音频」的样本里选一条；
        ③ 现场生成——无候选 / 未选中 / 选择失败 → Generator 现场生成智能回复。
          永不因选择失败而静默。
        未配置 LLM（fallback_replier 为 None）时返回 None（调用方不挂路由）。
        """
        p = self._profile
        if p is None or self._fallback_replier is None:
            return None
        text = str((context or {}).get("content") or "").strip()
        if not text:
            return None
        samples = p.get("samples_ai") or []
        # 第一级：关键词直出——弹幕命中样本 examples/type_name 直接播，不走 LLM
        hit_id = self._keyword_match(text, samples)
        if hit_id:
            reply = self._resolve_ai_sample(samples, hit_id, context)
            if reply:
                self._note_recent(hit_id)
                if self._stats is not None:
                    self._stats.inc("script_ai_keyword")
                self._log.info("script_ai_keyword", {
                    "user": (context or {}).get("user", ""), "chosen": hit_id,
                    "matched_audio": bool(reply.get("audio_path"))})
                return reply
        # 第二级（兜底）：LLM 从样本库选一条（只从有当前音色音频的样本中选择）
        candidates = self._ai_candidates(samples)
        if candidates and self._llm_selector is not None:
            chosen = ""
            try:
                chosen = await self._llm_selector(text, candidates) or ""
            except Exception as e:
                if self._stats is not None:
                    self._stats.inc("script_ai_select_error")
                self._log.warning("script_ai_select_failed",
                                  {"error": f"{type(e).__name__}: {e}"})
            if chosen:
                reply = self._resolve_ai_sample(samples, chosen, context)
                if reply:
                    self._note_recent(chosen)
                    if self._stats is not None:
                        self._stats.inc("script_ai_pick")
                    self._log.info("script_ai_pick", {
                        "user": (context or {}).get("user", ""), "chosen": chosen,
                        "matched_audio": bool(reply.get("audio_path"))})
                    return reply
        if self._stats is not None:
            self._stats.inc("script_ai_generate")
        self._log.info("script_ai_generate", {
            "user": (context or {}).get("user", ""), "content": text[:40],
            "candidates": len(candidates)})
        return await self._fallback_replier(context)

    def _keyword_match(self, text: str, samples: list) -> str | None:
        """关键词直出（第一级）：弹幕命中某样本的 examples/type_name 时直接返回
        sample:id，不走 LLM 选择。归一化后「互含」（弹幕含短语或短语含弹幕）优先，
        其次 2-gram 相似度 ≥ KEYWORD_SIM_THRESHOLD；剔除最近播过（防复读）与无内容样本。"""
        t = _norm_keyword(text)
        if not t:
            return None
        self._prune_recent()
        best_id, best_score = None, 0.0
        for s in samples:
            if not s.get("enabled"):
                continue
            if not (str(s.get("reply_text") or "").strip() or self._resolve_audio(s)):
                continue  # 既无文本又无当前音色音频的样本不参与
            sid = f"sample:{s.get('id')}"
            if sid in self._recent:
                continue  # 防复读：最近播过的样本跳过
            phrases = [str(x).strip() for x in (s.get("examples") or []) if str(x).strip()]
            tn = str(s.get("type_name") or "").strip()
            if tn:
                phrases.append(tn)
            score = 0.0
            for p in phrases:
                pn = _norm_keyword(p)
                if not pn:
                    continue
                if pn in t or t in pn:
                    score = 1.0
                    break
                score = max(score, default_similarity(pn, t))
            if score >= KEYWORD_SIM_THRESHOLD and score > best_score:
                best_id, best_score = sid, score
        return best_id

    def _ai_candidates(self, samples: list) -> list:
        """LLM 选择候选：只从「有当前直播音色音频且未过期」的启用样本中取
        （无候选则走现场生成），剔除最近播过的（防复读），上限 MAX_SELECT_CANDIDATES。
        """
        self._prune_recent()
        cands = []
        for s in samples:
            if not s.get("enabled"):
                continue
            if not self._resolve_audio(s):
                continue  # 发声链路统一走存档当前音色的预制音频
            reply = str(s.get("reply_text") or "").strip()
            examples = [e for e in (s.get("examples") or []) if str(e or "").strip()][:3]
            match = str(s.get("type_name") or "").strip()
            if examples:
                match += "（弹幕如：" + "；".join(examples) + "）"
            cands.append({"id": f"sample:{s.get('id')}", "kind": "sample",
                          "match": match, "reply": reply})
        fresh = [c for c in cands if c["id"] not in self._recent]
        return fresh[:MAX_SELECT_CANDIDATES]

    def _resolve_ai_sample(self, samples: list, chosen_id: str,
                           context: dict) -> dict | None:
        """选中的样本 → {"text","audio_path"}；无当前音色音频时 audio_path 空
        （播放侧回退 TTS 现场合成文本）。id 无法识别返回 None（按未选中）。"""
        kind, _, raw_id = str(chosen_id or "").partition(":")
        if kind != "sample":
            return None
        sample = next((s for s in samples if s.get("id") == raw_id), None)
        if sample is None or not sample.get("enabled"):
            return None
        reply_text = fill_reply_fields(sample.get("reply_text"),
                                       {"user": (context or {}).get("user", "")},
                                       self._prefix_len())
        audio = self._resolve_audio(sample)
        if not reply_text and not audio:
            return None
        return {"text": reply_text, "audio_path": audio}

    def _note_recent(self, chosen_id: str) -> None:
        self._recent[str(chosen_id)] = time.monotonic()

    def _prune_recent(self) -> None:
        if not self._recent:
            return
        now = time.monotonic()
        self._recent = {k: ts for k, ts in self._recent.items()
                        if now - ts < RECENT_PICK_WINDOW_S}

    # ---------- 内部 ----------

    def _resolve_audio(self, item: dict, voice_id: str = "", emotion: str = "") -> str:
        """条目 audios → 当前音色某情绪档的音频绝对路径（三级回落，DESIGN §8.3）。

        1. `audios[voice][emotion]` 存在且 text_hash 匹配 → 播该预录（延迟 0）；
        2. 该档**已在存档档位集合内**但缺音频 / hash 失配 → 回落 `default` 档预录；
        3. 该档**不在档位集合内**（情绪没预生成）→ 返回空串，由调用方走现场 TTS
           并带上该情绪——保留表现力自由度，代价是一次合成延迟。

        区分第 2、3 步是要点：已登记档位缺音频时保守回落 default（不额外花钱、
        不额外等待）；未登记档位则现场合成。

        无论哪一级，stale（条目当前文本 hash ≠ 引用 hash）都视为无音频：
        旧音频不播，由调用方回退现场 TTS 念新文本。
        """
        if not isinstance(item, dict) or not self._profile_id:
            return ""
        vid = str(voice_id or "").strip() or self.current_voice()
        if not vid:
            return ""
        entries = item.get("audios") or {}
        slots = entries.get(vid) if isinstance(entries, dict) else None
        if not isinstance(slots, dict):
            return ""
        emo = normalize_emotion(emotion)
        if emo != EMOTION_DEFAULT:
            path = self._slot_audio(slots.get(emo), item)
            if path:
                return path
            if emo not in self.current_emotions():
                return ""  # 未登记档位：交给调用方现场合成，不擅自降级为 default
            if self._stats is not None:
                self._stats.inc("emotion_fallback")
            self._log.info("sc_emotion_fallback", {
                "profile": self._profile_id, "voice": vid, "emotion": emo,
                "to": EMOTION_DEFAULT,
                "hint": "该档位已登记但缺音频，回落 default 预录"})
        return self._slot_audio(slots.get(EMOTION_DEFAULT), item)

    def _slot_audio(self, ref, item: dict) -> str:
        """单个情绪档的音频引用 → 绝对路径；stale / 文件缺失返回空串。"""
        if not isinstance(ref, dict):
            return ""
        name = str(ref.get("file") or "").strip()
        if not name:
            return ""
        text = (item.get("reply_text") if "reply_text" in item
                else item.get("text"))
        if str(ref.get("text_hash") or "") != text_hash(text):
            return ""
        try:
            path = self._store.audio_path(self._profile_id, name)
        except ScriptStoreError:
            return ""
        if not Path(path).is_file():
            self._log.warning("script_audio_missing", {"profile": self._profile_id,
                                                       "audio": name})
            return ""
        return str(path)

    def _prefix_len(self) -> int:
        return int((self._cfg.get("persona") or {}).get("nickname_prefix_len", 2))
