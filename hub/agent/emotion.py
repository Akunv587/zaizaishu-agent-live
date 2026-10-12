"""情绪档位与语速的校验与回落（DESIGN §8.6）。

决策契约里的 `emotion` 是**智能体自己选的**，可能是没预生成的档位、越界的枚举值、
甚至是写错的字符串。本模块负责把它收敛成播放链路能安全消费的取值，并给出
应记的日志事件（谁改成现场 TTS、谁被回落，决策日志里要能看出来）。

回落判据（DESIGN §8.6 逐条）：

| 输入 | 处置 | 日志事件 |
|---|---|---|
| 在档位集合内 | 直接用 | — |
| 在 8 枚举内但不在集合内 | 接受，走现场 TTS | `emotion_onsite` |
| 不在 8 枚举内 / 空 / 非法 | 回落 `default` | `emotion_invalid` |
| `agent.emotion.enabled = false` | 一律 `default` | — |
| `agent.emotion.onsite_fallback = false` 且档位未预生成 | 回落 `default` 预录（不走现场合成） | `emotion_onsite_blocked` |

语速 `speed`（DESIGN §5.3：0.5~2.0）在这里夹取。**实现口径**：决策没给或给了非数字时
返回 `None`（跟随音色档案参数），而不是强制 1.0——`SpeakTask.speed=None` 已是既有
"不覆盖"语义，强制 1.0 会抹掉音色工坊里用户调好的语速。显式数值越界则夹取并记
`speed_clamped`。
"""
from hub.config import TTS_EMOTIONS
from hub.scriptstore import EMOTION_DEFAULT, EMOTIONS_ALL

# 决策日志事件名
EVENT_ONSITE = "emotion_onsite"
EVENT_INVALID = "emotion_invalid"
EVENT_ONSITE_BLOCKED = "emotion_onsite_blocked"
EVENT_SPEED_CLAMPED = "speed_clamped"

SPEED_MIN = 0.5
SPEED_MAX = 2.0


def _norm_set(available) -> set:
    """档位集合归一：`default` 恒在（它永远可播），其余取 8 枚举。"""
    if isinstance(available, str):
        available = [available]
    picked = {str(e).strip().lower() for e in (available or []) if str(e).strip()}
    picked.add(EMOTION_DEFAULT)
    return picked


def resolve_emotion(value, available, enabled: bool = True,
                    onsite_fallback: bool = True) -> tuple:
    """返回 `(emotion, log_event)`；`log_event` 为空串表示无需记日志。"""
    if not enabled:
        return EMOTION_DEFAULT, ""
    e = str(value or "").strip().lower()
    if e not in EMOTIONS_ALL:
        return EMOTION_DEFAULT, EVENT_INVALID
    if e in _norm_set(available):
        return e, ""
    # 在 8 枚举内、但当前存档没勾这个档位
    if not onsite_fallback:
        return EMOTION_DEFAULT, EVENT_ONSITE_BLOCKED
    return e, EVENT_ONSITE


def resolve_speed(value, fallback: float = None) -> tuple:
    """返回 `(speed, log_event)`；给不出数字时返回 `fallback`（默认 None=不覆盖）。"""
    if value is None or isinstance(value, bool):
        return fallback, ""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return fallback, ""
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return fallback, ""
    clamped = max(SPEED_MIN, min(SPEED_MAX, f))
    if abs(clamped - f) > 1e-9:
        return round(clamped, 2), EVENT_SPEED_CLAMPED
    return round(clamped, 2), ""


def emotion_hint(available, onsite_fallback: bool = True) -> str:
    """DESIGN §8.6 的档位提示词段落（组 prompt 时附给 LLM）。"""
    picked = _norm_set(available)
    # 按 8 枚举 + default 的固定顺序列出，读起来稳定、便于肉眼核对日志
    ordered = [EMOTION_DEFAULT] + [e for e in TTS_EMOTIONS if e in picked]
    line = "可用情绪档位：" + ", ".join(ordered)
    if onsite_fallback:
        line += ("\n（选 default 或上述档位可立即出声；选其他情绪将现场合成，延迟约 2~4 秒）")
    else:
        line += "\n（只能选上述档位；选其他情绪将被忽略并改用 default）"
    return line
