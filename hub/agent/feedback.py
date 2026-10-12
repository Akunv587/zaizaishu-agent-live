"""反馈进化（DESIGN §7.2③，M6）：把主播对 AI 回复的判断沉淀成可召回的 few-shot 样本。

主播在直播中枢的每条 AI 回复旁点三个按钮之一：

| 按钮 | 动作 | `source` | 进 few-shot？ |
|---|---|---|---|
| 对勾 | 采纳 | `adopted` | **进** |
| 铅笔 | 改写 | `rewritten` | **进**（存的是改后文本，原文留档在 `original_reply`） |
| 叉   | 弃用 | `rejected` | **不进**（只留原因标签供统计） |

落盘两份：

- `profiles/<pid>/agent/examples.jsonl` —— 样本库（三种 source 都在这，注入时过滤）；
  字段形状对齐 `L2Retriever.index()` 的 `examples` 契约（`reply` + `event.content`），
  所以回流只需把文件读出来喂给检索层，不做二次转换
- `profiles/<pid>/agent/feedback.jsonl` —— 原始反馈流水（append-only 审计口径，含弃用原因）

两条硬规矩：

1. **写失败不向上抛**——反馈是旁路观测，不能反噬直播链路（与 `DecisionLogger` 同一原则）。
   但**用户要知道**：写失败会把异常记进 warning 并返回 `ok: False` 的结果，由 webui 弹提示。
2. **现读不缓存**——样本库是主播边播边攒的，几 KB 级文件每次现读（每次决策 <1ms），
   换"采纳完下一条立刻生效"，不做失效逻辑。
"""
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

from hub.logger import get_logger
from hub.paths import EXE_DIR

# 弃用原因标签（DESIGN §7.2③ 定死的四个；前端弹窗同源）
REASONS = ("不像主播", "太长", "跑题", "重复")
SOURCE_ADOPTED = "adopted"
SOURCE_REWRITTEN = "rewritten"
SOURCE_REJECTED = "rejected"
# 进 few-shot 的来源（rejected 只留档不参与召回）
FEW_SHOT_SOURCES = (SOURCE_ADOPTED, SOURCE_REWRITTEN)

EXAMPLES_FILE = "examples.jsonl"
FEEDBACK_FILE = "feedback.jsonl"
# 样本库上限：超过后最旧的被拒之门外（不是删除——文件里仍在，只是不再注入）。
# 防的是主播攒了几千条后每次决策都要打分几千条候选。
MAX_EXAMPLES = 300
MAX_TEXT_CHARS = 200
_ID_RE = re.compile(r"^fb_[0-9a-z]{6,}$")


class FeedbackError(Exception):
    """反馈输入非法（前端表单问题）——由调用方回显给用户，不进决策链路。"""


def _clip(text, limit: int = MAX_TEXT_CHARS) -> str:
    return str(text if text is not None else "").strip().replace("\n", " ")[:limit]


def _new_id(clock) -> str:
    """`fb_<36 进制毫秒><4 位随机>`：同毫秒连点也不撞。"""
    n = int(clock() * 1000)
    tail = f"{int(clock() * 1e6) % 1296:04x}"
    tail = "".join("0123456789abcdefghijklmnopqrstuvwxyz"[int(tail[i:i + 2], 36) % 36]
                   for i in (0, 2))
    return f"fb_{n:x}{tail}"


class FeedbackStore:
    """样本库与反馈流水的读写。构造一次长期复用；活动存档切换后自动跟随。"""

    def __init__(self, root=None, pid_provider=None, clock=None, log=None):
        self._root = Path(root) if root else (EXE_DIR / "profiles")
        self._pid_provider = pid_provider     # callable -> 活动存档 id（无则用 default）
        self._clock = clock or time.time
        self._log = log or get_logger("agent.feedback")

    # ---------- 路径 ----------

    @property
    def pid(self) -> str:
        pid = ""
        if self._pid_provider is not None:
            try:
                pid = str(self._pid_provider() or "").strip()
            except Exception:
                pid = ""
        return pid or "default"

    def _dir(self) -> Path:
        return self._root / self.pid / "agent"

    def examples_path(self) -> Path:
        return self._dir() / EXAMPLES_FILE

    def feedback_path(self) -> Path:
        return self._dir() / FEEDBACK_FILE

    # ---------- 写 ----------

    def adopt(self, entry: dict) -> dict:
        """采纳一条回复：原样进样本库。"""
        return self._record(SOURCE_ADOPTED, entry)

    def reject(self, entry: dict, reasons, note: str = "") -> dict:
        """弃用一条回复：只留档与原因标签，不进 few-shot。"""
        reasons = self._clean_reasons(reasons)
        if not reasons and not _clip(note):
            raise FeedbackError("弃用要选至少一个原因（或写一句备注）")
        return self._record(SOURCE_REJECTED, entry, reasons=reasons, note=note)

    def rewrite(self, entry: dict, text: str, note: str = "") -> dict:
        """改写一条回复：改后版本进样本库，原文留在 `original_reply`。"""
        reply = _clip(text)
        if not reply:
            raise FeedbackError("改写后的回复不能为空")
        return self._record(SOURCE_REWRITTEN, entry, reply=reply, note=note)

    def _record(self, source: str, entry: dict, reply: str = "",
                reasons=None, note: str = "") -> dict:
        entry = dict(entry or {})
        original = _clip(entry.get("reply"))
        text = reply or original
        if not text:
            raise FeedbackError("反馈要带回复原文（页面上的那条 AI 回复）")
        event_type = _clip(entry.get("event_type"), 20) or "chat"
        content = _clip(entry.get("content"))
        if event_type == "gift":
            name = _clip(entry.get("gift_name"), 40) or "礼物"
            content = content or f"{name} ×{int(entry.get('gift_count') or 1)}"
        record = {
            "id": _new_id(self._clock),
            "ts": datetime.fromtimestamp(self._clock()).isoformat(
                timespec="milliseconds"),
            "source": source,
            "event_type": event_type,
            "user": _clip(entry.get("user"), 40) or "观众",
            # 检索层的匹配文本：弹幕用内容，礼物用「礼物名 ×数量」
            "event": {"content": content},
            "event_line": _clip(entry.get("event_line")) or f"{event_type} {content}",
            "reply": text,
            "tone": _clip(entry.get("tone"), 20),
            "reason": _clip(entry.get("reason")),          # 智能体当时的判断依据（留档）
            "original_reply": original if source == SOURCE_REWRITTEN else "",
            "reasons": list(reasons or []),
            "note": _clip(note),
        }
        self._append(self.examples_path(), record)
        # 流水另记一份原始口径（含前端给的完整 entry，便于事后排查）
        audit = dict(record)
        audit["kind"] = {"adopted": "adopt", "rewritten": "rewrite",
                         "rejected": "reject"}[source]
        audit["entry"] = {k: _clip(v, 400) for k, v in entry.items()
                          if isinstance(v, (str, int, float))}
        self._append(self.feedback_path(), audit)
        self._log.info("agent_feedback_recorded",
                       {"id": record["id"], "source": source, "pid": self.pid,
                        "event_type": event_type, "reasons": record["reasons"]})
        return record

    def _append(self, path: Path, payload: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError as e:
            self._log.warning("agent_feedback_write_failed",
                              {"path": str(path), "error": f"{type(e).__name__}: {e}"})
            raise FeedbackError(f"反馈写入失败：{e}") from e

    @staticmethod
    def _clean_reasons(reasons) -> list:
        """只留已知标签；未知标签丢弃（防前端塞任意文案进样本库）。"""
        if isinstance(reasons, str):
            reasons = [reasons]
        if not isinstance(reasons, (list, tuple)):
            return []
        seen = []
        for r in reasons:
            text = str(r or "").strip()
            if text in REASONS and text not in seen:
                seen.append(text)
        return seen

    # ---------- 读 ----------

    def _read_jsonl(self, path: Path) -> list:
        if not path.is_file():
            return []
        out = []
        try:
            lines = path.read_text(encoding="utf-8").split("\n")
        except OSError as e:
            self._log.warning("agent_feedback_read_failed",
                              {"path": str(path), "error": str(e)})
            return []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue          # 半行/脏行跳过：样本库宁可少一条也不能让决策炸
            if isinstance(item, dict):
                out.append(item)
        return out

    def all_examples(self) -> list:
        """样本库全量（含 rejected，供统计）。"""
        return self._read_jsonl(self.examples_path())

    def examples(self) -> list:
        """few-shot 可用样本：只留 `adopted` / `rewritten`，且带非空回复。

        这是喂给 `L2Retriever.index(examples=...)` 的那份——形状与它的契约一致，
        不做二次转换。上限 `MAX_EXAMPLES`，超了取最新的（新样本比旧样本更贴近
        主播当下的口味）。
        """
        usable = [e for e in self.all_examples()
                  if e.get("source") in FEW_SHOT_SOURCES
                  and str(e.get("reply") or "").strip()]
        return usable[-MAX_EXAMPLES:]

    def stats(self) -> dict:
        """反馈进化区四项：样本总数 / 本周新增 / 采纳率 / 原因标签分布。"""
        items = self.all_examples()
        by_source = {s: 0 for s in (SOURCE_ADOPTED, SOURCE_REWRITTEN, SOURCE_REJECTED)}
        reasons: dict = {}
        now = self._clock()
        week = now - timedelta(days=7).total_seconds()
        week_added = 0
        for it in items:
            src = it.get("source")
            if src in by_source:
                by_source[src] += 1
            for r in it.get("reasons") or []:
                reasons[r] = reasons.get(r, 0) + 1
            ts = _parse_ts(it.get("ts"))
            if ts is not None and ts >= week:
                week_added += 1
        judged = by_source[SOURCE_ADOPTED] + by_source[SOURCE_REJECTED]
        return {
            "pid": self.pid,
            "total": len(items),
            "usable": by_source[SOURCE_ADOPTED] + by_source[SOURCE_REWRITTEN],
            "adopted": by_source[SOURCE_ADOPTED],
            "rewritten": by_source[SOURCE_REWRITTEN],
            "rejected": by_source[SOURCE_REJECTED],
            "week_added": week_added,
            # 改写单列不折进采纳率：它语义上是"原文被否定，但提供了新样本"
            "adopt_rate": round(by_source[SOURCE_ADOPTED] / judged * 100, 1) if judged else 0.0,
            "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "max_examples": MAX_EXAMPLES,
        }


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def valid_id(value: str) -> bool:
    """页面传回的样本 id 形态校验（现在只做形状检查，留作将来按 id 去重/撤销）。"""
    return bool(_ID_RE.fullmatch(str(value or "").strip()))


__all__ = ["FeedbackError", "FeedbackStore", "REASONS", "SOURCE_ADOPTED",
           "SOURCE_REJECTED", "SOURCE_REWRITTEN", "FEW_SHOT_SOURCES",
           "MAX_EXAMPLES", "valid_id"]
