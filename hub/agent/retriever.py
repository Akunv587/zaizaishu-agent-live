"""检索层（DESIGN §9）：L1 素材直注 + L2 关键词召回，L3 预留接口。

| 层 | 机制 | 本次 |
|---|---|---|
| **L1 素材直注** | 人设/知识/风格参数 + 话题话风 + 最多 N 条样本示例，全量拼进 system prompt | 实现（复用 `ScriptRuntime.build_system_prompt`，见 `prompt.py`） |
| **L2 关键词粗筛** | 复用 `hub.router.default_similarity` 的字符 2-gram Jaccard，召回 top-K | 实现（本模块） |
| **L3 语义向量 RAG** | 切块 → embedding → 向量索引 → 语义召回 | **不做，仅预留接口**（触发条件：素材 > 100 条） |

接口契约（DESIGN §9.2）：`index(profile, examples)` + `retrieve(query, top_k)`，
返回 `[{"id","kind","match","reply","score","has_audio"}]`。
`brain.py` 只依赖这个接口，将来换 L3 只需替换实现类。

两条硬约束（DESIGN §9.3）：

1. **音频不进检索**——检索的是文本知识，音频只是"用什么声音说"的载体；但结果必须带
   `has_audio` 标注（复用 `selector._resolve_audio` 的 stale 语义：文本 hash 失配视为无音频）。
   实现上把 `_resolve_audio` 作为 `audio_resolver` 注入，本模块不碰存档结构。
2. **音色走存档 `current_voice`**——智能体不换人说话，故 `has_audio` 一律按当前音色判定。

去重复用 `selector.RECENT_PICK_WINDOW_S = 600`：确实**说出去**的素材在窗口内不再被召回
（防复读）。判据是"回复文本与该素材的 reply 相似"，与 selector 的「最近选播候选」同源。
"""
import time

from hub.logger import get_logger

# 与 hub.selector.RECENT_PICK_WINDOW_S 同值：最近说过的素材在窗口内不再召回
RECENT_PICK_WINDOW_S = 600
# 判定"这句话就是说出去的这条素材"的相似度阈值（与弹幕合并阈值同源）
USED_SIM_THRESHOLD = 0.6
# 单条素材进入匹配文本的最大字符数（防一条超长话题拖慢每次决策的打分）
MAX_MATCH_CHARS = 160


class Retriever:
    """检索层统一接口。L2 与未来的 L3 都实现它。"""

    def index(self, profile: dict = None, examples: list = None) -> None:
        raise NotImplementedError

    def retrieve(self, query: str, top_k: int = 5) -> list:
        raise NotImplementedError


def _clip(text, limit: int = MAX_MATCH_CHARS) -> str:
    s = str(text if text is not None else "").strip()
    return s if len(s) <= limit else s[:limit]


class L2Retriever(Retriever):
    """关键词粗筛：毫秒级、零依赖、零外部服务。"""

    def __init__(self, top_k: int = 5, clock=None, similarity=None,
                 audio_resolver=None, log=None,
                 recent_window_s: float = RECENT_PICK_WINDOW_S):
        self._top_k = max(1, int(top_k))
        self._clock = clock or time.monotonic
        self._log = log or get_logger("agent.retriever")
        self._entries: list = []
        self._recent: dict = {}      # entry id -> monotonic 时间（最近说出去过）
        self._recent_window_s = float(recent_window_s)
        self._audio_resolver = audio_resolver
        if similarity is None:
            from hub.router import default_similarity
            similarity = default_similarity
        self._similarity = similarity

    @property
    def size(self) -> int:
        return len(self._entries)

    def entry_item(self, entry_id: str) -> dict | None:
        """按 id 取回素材的原始条目（存档条目 / 反馈样本）。

        `retrieve()` 按 DESIGN §9.2 的契约只返回 6 个字段，**不把原始条目漏出去**；
        需要原始条目时走这里取——目前唯一用途是"这条回复若命中召回素材，就去解析
        它的预录音频"（音频不进检索结果，但要用它的 id 反查，见 §9.3）。
        """
        for e in self._entries:
            if e["id"] == entry_id:
                return e.get("item") or {}
        return None

    # ---------- 建索引 ----------

    def index(self, profile: dict = None, examples: list = None) -> None:
        """重建索引（存档切换/编辑/反馈回流后调用）。

        L2 的实现就是"把素材摊平成一张表"，无外部索引；每次决策重建的成本
        与素材条数同阶（百级素材 < 1 ms），换取"存档一改立刻生效"。
        """
        p = profile or {}
        entries = []
        for s in p.get("samples_ai") or []:
            if not s.get("enabled") or not str(s.get("reply_text") or "").strip():
                continue
            examples_text = " ".join(str(x) for x in (s.get("examples") or []) if str(x).strip())
            entries.append({
                "id": f"sample:{s.get('id', '')}",
                "kind": "sample",
                "match": _clip(examples_text or s.get("type_name", "")),
                "reply": str(s.get("reply_text") or "").strip(),
                "item": s,
            })
        for t in p.get("topics_ai") or []:
            if not t.get("enabled") or not str(t.get("text") or "").strip():
                continue
            text = str(t.get("text") or "").strip()
            entries.append({
                "id": f"topic:{t.get('id', '')}",
                "kind": "topic",
                "match": _clip(text),
                "reply": text,
                "item": t,
            })
        for ex in examples or []:
            reply = str((ex or {}).get("reply") or "").strip()
            if not reply:
                continue
            event = (ex or {}).get("event") or {}
            entries.append({
                "id": f"example:{ex.get('id', '')}",
                "kind": "example",
                "match": _clip(event.get("content", "")),
                "reply": reply,
                "item": ex,
            })
        self._entries = entries
        self._log.debug("agent_index_rebuilt", {"entries": len(entries)})

    # ---------- 召回 ----------

    def retrieve(self, query: str, top_k: int = None) -> list:
        """按 2-gram Jaccard 召回 top-K；最近说过的素材在窗口内被剔除。"""
        text = str(query or "").strip()
        if not text or not self._entries:
            return []
        self._purge_recent()
        scored = []
        for e in self._entries:
            if e["id"] in self._recent:
                continue
            score = self._score(text, e)
            if score > 0:
                scored.append((score, e))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        limit = self._top_k if top_k is None else max(1, int(top_k))
        return [self._shape(e, score) for score, e in scored[:limit]]

    def note_spoken(self, text: str, threshold: float = None) -> list:
        """把"确实说出去的那条素材"记进最近窗口（防复读）；返回命中的 id。"""
        spoken = str(text or "").strip()
        if not spoken:
            return []
        limit = USED_SIM_THRESHOLD if threshold is None else float(threshold)
        now = self._clock()
        hit = []
        for e in self._entries:
            if e["id"] in self._recent:
                continue
            if e["reply"] == spoken or self._similarity(e["reply"], spoken) >= limit:
                self._recent[e["id"]] = now
                hit.append(e["id"])
        if hit:
            self._log.info("agent_recall_marked_spoken", {"ids": hit})
        return hit

    def clear_recent(self) -> None:
        """开播/切场次时清掉去重窗口（记忆清空时一并调用）。"""
        self._recent.clear()

    # ---------- 内部 ----------

    def _score(self, query: str, entry: dict) -> float:
        best = 0.0
        for field in ("match", "reply"):
            value = entry.get(field) or ""
            if not value:
                continue
            try:
                best = max(best, float(self._similarity(query, value)))
            except Exception:  # 相似度函数异常不能反噬召回
                continue
        return best

    def _shape(self, entry: dict, score: float) -> dict:
        return {
            "id": entry["id"],
            "kind": entry["kind"],
            "match": entry["match"],
            "reply": entry["reply"],
            "score": round(float(score), 4),
            "has_audio": self._has_audio(entry),
        }

    def _has_audio(self, entry: dict) -> bool:
        """该素材在当前直播音色下有可播音频（复用 selector._resolve_audio 语义）。"""
        if self._audio_resolver is None:
            return False
        try:
            return bool(str(self._audio_resolver(entry.get("item") or {}, "") or "").strip())
        except Exception:
            return False

    def _purge_recent(self) -> None:
        if not self._recent:
            return
        now = self._clock()
        self._recent = {k: ts for k, ts in self._recent.items()
                        if now - ts < self._recent_window_s}
