"""内部事件模型与归一化（PROMPTS 步骤 1.3，DESIGN §5.1/§4.1）。

LiveEvent / SpeakTask 两个 dataclass；normalize 把 Dycast 的 DyMessage 原始 dict
转为 LiveEvent。任何必需字段取不到返回 None 并记 warning，不抛异常。
"""
import time
from dataclasses import dataclass, field

from hub.logger import get_logger

METHOD_TO_TYPE = {
    "WebcastChatMessage": "chat",
    "WebcastGiftMessage": "gift",
    "WebcastSocialMessage": "follow",
    "WebcastFansclubMessage": "fansclub",
    "WebcastMemberMessage": "enter",
}


@dataclass
class LiveEvent:
    type: str            # chat / gift / follow / fansclub / enter
    user: str            # 观众昵称
    content: str = ""    # 弹幕文本或粉丝团附言
    gift_name: str = ""
    gift_count: int = 1
    gift_value: int = 0  # 原始 price 透传；单位口径见 OQ-1（换算集中在 normalize 单点）
    combo_end: bool = False  # Dycast gift.repeatEnd 透传，连击结束标记
    ts: float = 0.0      # 事件到达时间戳（秒）
    test: bool = False   # 控制台测试事件标记（router 据此绕过冷却/概率/enter 开关）


@dataclass
class SpeakTask:
    priority: int        # 0=礼物 1=互动 2=口播
    text: str            # 待合成话术
    event_ts: float      # 源事件时间戳，用于过期判断
    source: str          # chat / gift / follow / fansclub / schedule / watchdog
    merged_count: int = 1  # 聚合合并数（chat 聚合窗口用）
    tier: str = ""       # 礼物分档：low / mid / high（gift 结算用）
    interrupt: bool = False  # 是否插队（中、高档礼物）
    audio_path: str = ""  # 预录音频绝对路径（v4 话术存档：音色×情绪档）；非空时播放侧优先播文件，失败回退 TTS 念 text
    test: bool = False   # 测试事件产出的任务（webui 广播 test 标记，前端测试面板据 source 回显）
    # v4 情绪覆盖（DESIGN §8）：留空 = 跟随 tts 构造期参数；非 default 且无预录音频时
    # 播放侧现场 TTS 会带上它（档位未登记时按未登记档处理）
    emotion: str = ""
    speed: float | None = None   # 语速覆盖（0.5~2.0），None = 跟随 tts 配置
    # v4 反馈进化（DESIGN §7.2③，M6）：**仅智能体决策**产出的任务带它，用来在
    # 直播中枢那条回复旁挂「采纳 / 弃用 / 改写」三个按钮（rules 链路为 None）。
    # 形状 = `FeedbackStore` 的 entry（event_type / user / content / gift_* / tone /
    # reason / reply），前端原样回传即入样本库。
    feedback: dict | None = None


def _to_int(value, default: int) -> int:
    """gift.count / gift.price 容错 int 与 str 两种类型。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if s.isdigit() or (s[:1] in "+-" and s[1:].isdigit()):
            try:
                return int(s)
            except ValueError:
                return default
    return default


def _to_bool(value) -> bool:
    """gift.repeatEnd 容错 bool 与 int（及字符串 true/false）。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return False


def _warn(method: str, reason: str, dy: dict) -> None:
    get_logger("events").warning("normalize_field_missing", {
        "method": method, "reason": reason,
        "raw_keys": list(dy.keys()) if isinstance(dy, dict) else str(type(dy)),
    })


def normalize(dy: dict, ts: float = None) -> LiveEvent | None:
    """DyMessage 原始 dict → LiveEvent；非白名单或必需字段缺失返回 None。"""
    if ts is None:
        ts = time.time()
    if not isinstance(dy, dict):
        return None
    method = dy.get("method")
    ev_type = METHOD_TO_TYPE.get(method)
    if ev_type is None:
        return None

    user_obj = dy.get("user")
    user = str((user_obj or {}).get("name") or "").strip() if isinstance(user_obj, dict) else ""
    if not user:
        _warn(str(method), "user.name 缺失或为空", dy)
        return None

    ev = LiveEvent(type=ev_type, user=user, ts=ts, test=_to_bool(dy.get("test")))

    if ev_type == "chat":
        content = dy.get("content")
        if content is None or str(content).strip() == "":
            _warn(str(method), "content 缺失或为空", dy)
            return None
        ev.content = str(content)

    elif ev_type == "gift":
        gift = dy.get("gift")
        if not isinstance(gift, dict):
            _warn(str(method), "gift 对象缺失", dy)
            return None
        gift_name = str(gift.get("name") or "").strip()
        if not gift_name:
            _warn(str(method), "gift.name 缺失或为空", dy)
            return None
        ev.gift_name = gift_name
        ev.gift_count = _to_int(gift.get("count"), default=1)
        # TODO(OQ-1): price 的单位（音浪/抖币）与换算口径待步骤 2.4 用真实礼物数据确认，
        # 换算逻辑集中在本行，勿散落别处。
        ev.gift_value = _to_int(gift.get("price"), default=0)
        # TODO(OQ-2): repeatEnd 语义（中间消息是否携带累计 count）待步骤 2.4 确认。
        ev.combo_end = _to_bool(gift.get("repeatEnd"))

    elif ev_type == "fansclub":
        ev.content = str(dy.get("content") or "")

    return ev
