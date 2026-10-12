"""单场记忆（DESIGN §10.1）：进程内环形缓冲，不落盘。

三类缓冲：

| 缓冲 | 内容 | 用途 |
|---|---|---|
| `events` | 最近收到的 LiveEvent 摘要（类型/用户/内容/礼物/时间戳） | 判断连击、判断是否重复提问、感知直播间氛围 |
| `spoken` | 智能体最近说过的回复文本 | **防复读**——避免反复说同一句话 |
| `notes` | 智能体自己写入的临时笔记（决策的 `memory_note` 字段） | 记住本场的临时上下文 |

**容量是上下文窗口，不是限流**（DESIGN §10.1 注）：它决定喂给 LLM 的上下文长度
（token 成本与判断质量的平衡），不限制"回复多少条"，与本次"不加限流"的决策不冲突。

其余一律不落盘（DESIGN §10.2）：跨场次持久化的只有反馈样本库，在 `feedback.py`（M6）。
进程重启即清空。隐私考量：观众昵称与弹幕内容不会被长期留存——除了主播主动采纳为样本。
"""
from collections import deque
from datetime import datetime

from hub.logger import get_logger

# 出厂容量（与 config.yaml 的 agent.memory.* 默认一致，见 hub.config.agent_settings）
DEFAULT_MAX_EVENTS = 50
DEFAULT_MAX_SPOKEN = 20
DEFAULT_MAX_NOTES = 10
# 防复读判定的相似度阈值：与 router 的弹幕合并阈值同源（2-gram Jaccard）
SPOKEN_SIM_THRESHOLD = 0.6
# 单条事件摘要 / 笔记的最大字符数（防一条超长弹幕撑爆上下文）
MAX_CONTENT_CHARS = 120
MAX_NOTE_CHARS = 120

_TYPE_LABEL = {"chat": "弹幕", "gift": "礼物", "follow": "关注",
               "fansclub": "粉丝团", "enter": "进场"}


def _default_similarity(a: str, b: str) -> float:
    """2-gram Jaccard；复用 `hub.router.default_similarity`（惰性导入避免模块级循环）。"""
    from hub.router import default_similarity
    return default_similarity(a, b)


def _clip(text, limit: int) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _hhmmss(ts) -> str:
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return "--:--:--"
    if value <= 0:
        return "--:--:--"
    try:
        return datetime.fromtimestamp(value).strftime("%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "--:--:--"


class SessionMemory:
    """单场记忆；`max_notes=0` 是合法下界（等于关掉临时笔记）。"""

    def __init__(self, max_events: int = DEFAULT_MAX_EVENTS,
                 max_spoken: int = DEFAULT_MAX_SPOKEN,
                 max_notes: int = DEFAULT_MAX_NOTES,
                 log=None, similarity=None):
        # deque(maxlen=0) 合法：写入即丢弃，正好表达"关掉这一路缓冲"
        self._events = deque(maxlen=max(0, int(max_events)))
        self._spoken = deque(maxlen=max(0, int(max_spoken)))
        self._notes = deque(maxlen=max(0, int(max_notes)))
        self._log = log or get_logger("agent.memory")
        self._similarity = similarity or _default_similarity

    # ---------- 事件 ----------

    def add_event(self, ev) -> dict:
        """记录一条事件摘要（接受 LiveEvent 或等价的鸭子类型对象）。返回写入的摘要。

        注意：**`should_reply=false` 的事件同样要记**——否则智能体看不出
        "这条刚被别人问过"「同一用户连刷礼物」，判断依据就断了。
        """
        summary = {
            "type": str(getattr(ev, "type", "") or ""),
            "user": str(getattr(ev, "user", "") or ""),
            "content": _clip(getattr(ev, "content", ""), MAX_CONTENT_CHARS),
            "gift_name": str(getattr(ev, "gift_name", "") or ""),
            "gift_count": int(getattr(ev, "gift_count", 1) or 1),
            "gift_value": int(getattr(ev, "gift_value", 0) or 0),
            "ts": float(getattr(ev, "ts", 0.0) or 0.0),
        }
        self._events.append(summary)
        return summary

    def events(self, limit: int = None) -> list:
        """最近事件（按时间正序，最旧的在前——喂 LLM 时更符合阅读顺序）。"""
        items = list(self._events)
        return items[-limit:] if limit else items

    def event_lines(self, limit: int = None) -> list:
        """事件摘要渲染为提示词行，如「[12:03:46] 礼物 小明 送出 小心心 ×5」。"""
        lines = []
        for e in self.events(limit):
            label = _TYPE_LABEL.get(e["type"], e["type"] or "事件")
            head = f"[{_hhmmss(e['ts'])}] {label} {e['user']}"
            if e["type"] == "gift" and e["gift_name"]:
                head += f" 送出 {e['gift_name']} ×{e['gift_count']}"
                if e["gift_value"]:
                    head += f"（价值 {e['gift_value']}）"
            elif e["content"]:
                head += f"：{e['content']}"
            lines.append(head)
        return lines

    # ---------- 说过的 ----------

    def add_spoken(self, text: str) -> None:
        """记录一条说出去的回复（防复读的比对源）。"""
        s = _clip(text, MAX_CONTENT_CHARS)
        if s:
            self._spoken.append(s)

    def spoken(self, limit: int = None) -> list:
        items = list(self._spoken)
        return items[-limit:] if limit else items

    def is_repeated(self, text: str, threshold: float = None) -> bool:
        """这句话是不是最近说过（完全相同或 2-gram 相似度达阈值）。"""
        s = str(text or "").strip()
        if not s:
            return False
        limit = SPOKEN_SIM_THRESHOLD if threshold is None else float(threshold)
        for old in self._spoken:
            if old == s:
                return True
            try:
                if self._similarity(old, s) >= limit:
                    return True
            except Exception:  # 相似度函数不可用时退化为精确比对，绝不反噬决策
                continue
        return False

    # ---------- 笔记 ----------

    def add_note(self, text: str) -> None:
        s = _clip(text, MAX_NOTE_CHARS)
        if s and s not in self._notes:
            self._notes.append(s)

    def notes(self) -> list:
        return list(self._notes)

    # ---------- 组装 / 维护 ----------

    def brief(self) -> dict:
        """组装给提示词用的记忆快照——事件已渲染成人话行（见 `event_lines`）。

        这里返回**成品行**而不是事件 dict：`prompt.py` 是纯文本装配器，拿到的
        `memory_brief` 直接 `f"· {行}"` 拼进 system prompt，不做记忆对象的渲染。
        曾经这里返回原始事件 dict，导致提示词里出现
        `{'type': 'chat', 'user': '观众甲', 'gift_value': 0, 'ts': 1790049318.18}`
        这样的 Python repr：一条事件占一百多字符（人话版约 30 字），还夹着对模型
        毫无意义的浮点时间戳——十几条记忆就把上下文烧掉上千 token。
        """
        return {"events": self.event_lines(), "spoken": self.spoken(),
                "notes": self.notes()}

    def stats(self) -> dict:
        return {"events": len(self._events), "spoken": len(self._spoken),
                "notes": len(self._notes)}

    def clear(self) -> None:
        """开播/切场次时清空（进程内记忆不跨场）。"""
        self._events.clear()
        self._spoken.clear()
        self._notes.clear()
        self._log.info("agent_memory_cleared", None)
