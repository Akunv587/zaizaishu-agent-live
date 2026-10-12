"""决策契约校验与三级容错（DESIGN §5.3）。

LLM 被要求返回一段结构化 JSON：

```json
{
  "should_reply": true, "priority": 1, "interrupt": false,
  "tone": "热情", "emotion": "happy", "speed": 1.05,
  "reason": "新观众提问游戏玩法，属于值得回的有效弹幕",
  "reply_text": "兄弟这个游戏就是点点小手柄下载，进去挂着就行",
  "memory_note": "刚有个新观众问怎么下载，已经答过了"
}
```

真实模型不会每次都听话，实测常见的三种非法形态**都必须有出路**（T5）：

1. **截断**——JSON 写到一半断了（`max_tokens` 撞顶）：`{...` 不闭合；
2. **混入说明文字**——`好的，我的判断如下：{...} 以上就是我的回复`；
3. **完全非 JSON**——直接输出一句大白话。

容错链（`parse_decision`）：

| 级 | 触发 | 处置 | `degrade` |
|---|---|---|---|
| 0 | 纯 JSON（含 ```json 围栏包裹） | 正常解析 | `""` |
| 1 | 上下文中嵌着 JSON | 提取首个**括号配平**的 `{...}` 重解析 | `"extract"` |
| 2 | JSON 残缺（截断）但能抢救 | 正则抓出 `should_reply` / `reply_text` 的值，当作可用决策 | `"salvage"` |
| 3 | 完全非 JSON 的散文 | 纯文本当 `reply_text`，`priority=1`，`reason="解析降级"` | `"plain"` |
| 4 | 空响应 / JSON 残骸且抢救不出 / 清洗后无话可说 | **抛 `DecisionError`**，由 `brain` 走 `safety.fallback_reply` 兜底口播 | — |

> **第 2 级是 M4 落地时补的一档**（DESIGN §5.3 只写了三级）。理由：截断的 JSON
> 若照 DESIGN 直接降到"纯文本当 reply_text"，会把 `{"should_reply": true, "reply_`
> 这种残骸当话术**念给观众听**——不崩、不静默，但观感是灾难。故先尝试抢救
> `reply_text` / `should_reply` 两个字面量（未闭合也能用正则抓到），抢救不出再
> 判定为"JSON 残骸"直接进第 4 级走兜底口播，而不是把残骸说出去。

设计原则：**永不静默**（除未配置 LLM 与 `should_reply=false` 的主动不回）。

字段归一的口径：

- `should_reply` 缺字段 → 退化为"有 reply_text 就是要回"，不凭空吞掉一条回复；
- `should_reply=false` → 强制清空 `reply_text`，不给下游留可播文本（V4.3 的结构性保证）；
- `priority` 夹取到 0~2，非法回落 1；
- `interrupt` **仅在 `priority=0` 时生效**（DESIGN §5.3：插队是礼物级能力）；
- `speed` 只做数字归一，越界夹取交给 `emotion.resolve_speed`（本模块不判业务边界）。
"""
import json
import math
import re
from dataclasses import dataclass

from hub.prompt import clean_reply

PRIORITY_MIN = 0
PRIORITY_MAX = 2
DEFAULT_PRIORITY = 1
# reason 字段在降级时的固定文案（决策透视页会原样展示）
REASON_DEGRADED = "解析降级"
MAX_REASON_CHARS = 200
MAX_TONE_CHARS = 20
MAX_NOTE_CHARS = 120
# `degrade` 取值
DEGRADE_EXTRACT = "extract"
DEGRADE_SALVAGE = "salvage"
DEGRADE_PLAIN = "plain"

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)
# 截断 JSON 的抢救：值可能未闭合，所以匹配到未转义引号或文本末尾为止
_SALVAGE_REPLY_RE = re.compile(r'"reply_text"\s*:\s*"((?:[^"\\]|\\.)*)')
_SALVAGE_SHOULD_RE = re.compile(r'"should_reply"\s*:\s*(true|false)', re.I)
# 判定"这是 JSON 残骸而不是散文"：起手就是花括号，或出现了契约字段名
_DEBRIS_RE = re.compile(r'"\s*(should_reply|reply_text|priority|memory_note)\s*"')


class DecisionError(Exception):
    """模型输出无法成为一条可用决策（空响应 / 无话可说）——由调用方走兜底口播。"""


@dataclass
class AgentDecision:
    """一次决策的完整结果；`degrade` 非空表示走了容错降级。"""

    should_reply: bool = False
    priority: int = DEFAULT_PRIORITY
    interrupt: bool = False
    tone: str = ""
    emotion: str = ""
    speed: float | None = None
    reason: str = ""
    reply_text: str = ""
    memory_note: str = ""
    degrade: str = ""

    def has_reply(self) -> bool:
        """是否真有一条能播出去的话（`should_reply` 与文本双满足）。"""
        return bool(self.should_reply and self.reply_text)

    def to_record(self) -> dict:
        """落决策日志用的可序列化字典（字段名与契约一致，便于回放比对）。"""
        return {
            "should_reply": self.should_reply,
            "priority": self.priority,
            "interrupt": self.interrupt,
            "tone": self.tone,
            "emotion": self.emotion,
            "speed": self.speed,
            "reason": self.reason,
            "reply_text": self.reply_text,
            "memory_note": self.memory_note,
            "degrade": self.degrade,
        }


def _str(value, limit: int = 0) -> str:
    s = str(value if value is not None else "").strip()
    return s if not limit or len(s) <= limit else s[:limit]


def _as_bool(value, default=None):
    """三态布尔：认不出来时返回 default（供 `should_reply` 判定"字段是否缺失"）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, float):
        return value != 0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("true", "1", "yes", "y", "on"):
            return True
        if s in ("false", "0", "no", "n", "off"):
            return False
    return default


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        try:
            n = int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default
    return max(lo, min(hi, n))


def _to_speed(value) -> float | None:
    """只做数字归一；不判 0.5~2.0 边界（那是 emotion.resolve_speed 的职责）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _load_object(text: str) -> dict | None:
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _extract_object(text: str) -> str | None:
    """取首个括号配平的 `{...}` 块；跳过字符串字面量内的花括号与转义。

    比"第一个 { 到最后一个 }"稳：混入说明文字时后者会把尾部的散文一起圈进来，
    导致 `json.loads` 失败、白白降到纯文本级。
    """
    start = text.find("{")
    while start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def _from_mapping(data: dict, degrade: str, max_reply_chars: int) -> AgentDecision:
    reply_text = clean_reply(data.get("reply_text"), max_reply_chars)
    should_reply = _as_bool(data.get("should_reply"), default=None)
    if should_reply is None:
        should_reply = bool(reply_text)
    if not should_reply:
        reply_text = ""  # 主动不回：不给下游留可播文本
    priority = _clamp_int(data.get("priority"), DEFAULT_PRIORITY,
                          PRIORITY_MIN, PRIORITY_MAX)
    interrupt = bool(_as_bool(data.get("interrupt"), False)) and priority == 0
    return AgentDecision(
        should_reply=should_reply,
        priority=priority,
        interrupt=interrupt,
        tone=_str(data.get("tone"), MAX_TONE_CHARS),
        emotion=_str(data.get("emotion")),
        speed=_to_speed(data.get("speed")),
        reason=_str(data.get("reason"), MAX_REASON_CHARS),
        reply_text=reply_text,
        memory_note=_str(data.get("memory_note"), MAX_NOTE_CHARS),
        degrade=degrade,
    )


def _looks_like_debris(text: str) -> bool:
    """是不是一段没写完的 JSON（而不是一句人话）。"""
    stripped = text.lstrip()
    return stripped.startswith("{") or bool(_DEBRIS_RE.search(text))


def _salvage_reply_text(text: str) -> str:
    """从残缺 JSON 里抓 `reply_text` 的值（未闭合也能抓）。抓不到返回空串。"""
    m = _SALVAGE_REPLY_RE.search(text)
    if not m:
        return ""
    raw = m.group(1)
    try:  # 抓到的片段可能含 \" \n 等转义，能反转义就反转义
        return str(json.loads(f'"{raw}"'))
    except (ValueError, TypeError):
        return raw.replace('\\"', '"').replace("\\n", " ").strip()


def parse_decision(raw, max_reply_chars: int = 40) -> AgentDecision:
    """把模型原始输出解析成 `AgentDecision`；解析不出可播内容时抛 `DecisionError`。"""
    text = str(raw if raw is not None else "").strip()
    if not text:
        raise DecisionError("模型返回为空")

    fenced = _FENCE_RE.search(text)
    if fenced:
        data = _load_object(fenced.group(1).strip())
        if data is not None:
            return _from_mapping(data, "", max_reply_chars)

    data = _load_object(text)
    if data is not None:
        return _from_mapping(data, "", max_reply_chars)

    inner = _extract_object(text)
    if inner is not None:
        data = _load_object(inner)
        if data is not None:
            return _from_mapping(data, DEGRADE_EXTRACT, max_reply_chars)

    # 第 2 级：JSON 残缺（截断）——先听模型有没有明确说"不回"，再抢救它的话
    should = _SALVAGE_SHOULD_RE.search(text)
    if should and should.group(1).lower() == "false":
        return AgentDecision(should_reply=False, reason=REASON_DEGRADED,
                             degrade=DEGRADE_SALVAGE)
    salvaged = _salvage_reply_text(text)
    if salvaged:
        reply = clean_reply(salvaged, max_reply_chars)
        if reply:
            return AgentDecision(should_reply=True, priority=DEFAULT_PRIORITY,
                                 reason=REASON_DEGRADED, reply_text=reply,
                                 degrade=DEGRADE_SALVAGE)

    # 第 3 级：完全是散文——但别把 JSON 残骸当话术念出去（第 4 级走兜底口播）
    if _looks_like_debris(text):
        raise DecisionError("JSON 残缺且抢救不出回复文本")
    reply = clean_reply(text, max_reply_chars)
    if not reply:
        raise DecisionError("输出既非 JSON、也无法作为口播文本")
    return AgentDecision(should_reply=True, priority=DEFAULT_PRIORITY,
                         reason=REASON_DEGRADED, reply_text=reply,
                         degrade=DEGRADE_PLAIN)
