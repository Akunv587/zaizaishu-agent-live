"""智能体 system prompt 组装（DESIGN §4.2 / §8.6 / §10.1）。

组装顺序（前四段是**素材**，最后一段是**输出契约**）：

1. 人设知识库 —— 复用 `ScriptRuntime.build_system_prompt()`：它已经把「人设/游戏知识/
   行为规则」+「话题话风参考」+「L1 样本 few-shot 示例」拼好了（DESIGN 的 L1 素材直注）。
   此处不重复实现，直接吃它的结果当底料。
2. L2 召回结果 —— 与当前事件最相关的素材（带 `has_audio` 标注）。
3. 本场记忆 —— 最近事件 / 最近说过的话（防复读）/ 临时笔记（DESIGN §10.1）。
4. 可用情绪档位 —— DESIGN §8.6 要求必须列给 LLM，否则它会选没预生成的档、白等 2~4 秒。
5. 输出契约 —— DESIGN §5.3 的 JSON 字段说明 + 一条示例。

**提示词里不放音频**：检索的是文本知识，音频只是"用什么声音说"的载体（DESIGN §9.3）。
"""
from hub.agent.emotion import emotion_hint

# 召回素材最多拼几条（防素材多了把上下文撑爆；L1 已有 20 条 few-shot）
MAX_RECALL_LINES = 8
# 单条召回素材的 match / reply 截断长度
MAX_RECALL_CHARS = 80
# 记忆段最多拼几行事件 / 几句话
MAX_MEMORY_EVENT_LINES = 12
MAX_MEMORY_SPOKEN_LINES = 8

_KIND_LABEL = {"sample": "样本", "topic": "话题", "example": "好评样本"}
_EVENT_LABEL = {"chat": "弹幕", "gift": "礼物", "follow": "关注",
                "fansclub": "粉丝团", "enter": "进场"}
_GIFT_TIER_LABEL = {"low": "小额", "mid": "中额", "high": "大额"}


def _clip(text, limit: int = MAX_RECALL_CHARS) -> str:
    s = str(text if text is not None else "").strip().replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def describe_event(context: dict) -> str:
    """把事件上下文渲染成一句人话（同一份口径也用于决策日志与试玩页）。"""
    ctx = context or {}
    kind = str(ctx.get("type") or "chat")
    user = str(ctx.get("user") or "观众")
    if kind == "gift":
        name = str(ctx.get("gift_name") or "礼物")
        count = int(ctx.get("gift_count") or 1)
        value = int(ctx.get("gift_value") or 0)
        tier = _GIFT_TIER_LABEL.get(str(ctx.get("tier") or ""), "")
        text = f"{user} 送出 {name} ×{count}"
        if value:
            text += f"（价值 {value}）"
        if tier:
            text += f"，属于{tier}礼物"
        return text
    if kind == "follow":
        return f"{user} 关注了直播间"
    if kind == "fansclub":
        content = _clip(ctx.get("content"), 60)
        return f"{user} 加入了粉丝团" + (f"，附言：{content}" if content else "")
    if kind == "enter":
        return f"{user} 进入了直播间"
    members = ctx.get("members") or []
    if members:
        body = "；".join(f"{u}：{c}" for u, c in members)
    else:
        body = f"{user}：{ctx.get('content', '')}"
    merged = int(ctx.get("merged_count") or 1)
    if merged > 1:
        body += f"（另有 {merged - 1} 条相同弹幕，一并回应）"
    return body


def _recall_section(recalls) -> str:
    lines = []
    for r in list(recalls or [])[:MAX_RECALL_LINES]:
        label = _KIND_LABEL.get(str(r.get("kind") or ""), "素材")
        audio = "·有预录音频" if r.get("has_audio") else ""
        match = _clip(r.get("match"))
        reply = _clip(r.get("reply"))
        head = f"- （{label}{audio}）"
        if match:
            head += f"适用：{match} ｜ "
        lines.append(head + f"回复：{reply}")
    if not lines:
        return ""
    return ("【本轮素材召回】与当前事件相关的直播间素材（可参考语气与信息量，"
            "但不要原样照搬）\n" + "\n".join(lines))


def _memory_section(brief) -> str:
    brief = brief or {}
    events = list(brief.get("events") or [])[-MAX_MEMORY_EVENT_LINES:]
    spoken = list(brief.get("spoken") or [])[-MAX_MEMORY_SPOKEN_LINES:]
    notes = list(brief.get("notes") or [])
    blocks = []
    if events:
        blocks.append("最近事件：\n" + "\n".join(f"· {e}" for e in events))
    if spoken:
        blocks.append("你最近说过的话（避免重复同一句话）：\n"
                      + "\n".join(f"· {_clip(s)}" for s in spoken))
    if notes:
        blocks.append("本场临时笔记：\n" + "\n".join(f"· {_clip(n)}" for n in notes))
    if not blocks:
        return ""
    return "【本场直播记忆】\n" + "\n".join(blocks)


def _contract(max_reply_chars: int) -> str:
    return (
        "【输出要求】\n"
        "只输出一个 JSON 对象，不要解释、不要前后缀、不要代码块标记。字段：\n"
        "- should_reply: 布尔值。这条事件值不值得回；不值得就 false，我不会说话"
        "（允许不回，不必硬凑）。\n"
        "- priority: 0 / 1 / 2。0 = 礼物级可插队，1 = 互动回复，2 = 闲时口播。\n"
        "- interrupt: 布尔值。仅 priority=0 时有效，true 表示立刻插到队首说。\n"
        "- tone: 字符串。本轮语气（如 热情 / 平和 / 俏皮），会写进决策日志。\n"
        "- emotion: 字符串。取值见上方「可用情绪档位」。\n"
        "- speed: 数字。语速，0.5 ~ 2.0。\n"
        "- reason: 字符串。一句话说明为什么这么回（会展示给主播看，请写清判断依据）。\n"
        f"- reply_text: 字符串。要说的口播内容，不超过 {max_reply_chars} 字；不回时留空字符串。\n"
        "- memory_note: 字符串，可选。值得本场记住的临时信息（如“刚答过下载问题”）。\n"
        "\n"
        "示例（字段齐全，照这个形状输出）：\n"
        '{"should_reply": true, "priority": 1, "interrupt": false, "tone": "热情", '
        '"emotion": "happy", "speed": 1.0, "reason": "新观众问怎么下载，还没答过", '
        '"reply_text": "兄弟点右下角小手柄就能下，进去挂着就行", '
        '"memory_note": "答过下载"}'
    )


def build_agent_system_prompt(base_prompt: str, recalls=None, memory_brief=None,
                              emotions=None, onsite_fallback: bool = True,
                              max_reply_chars: int = 40) -> str:
    """按上面的顺序拼一份完整的 system prompt。"""
    parts = [str(base_prompt or "").strip()]
    for block in (_recall_section(recalls),
                  _memory_section(memory_brief),
                  "【可用情绪档位】\n" + emotion_hint(emotions, onsite_fallback),
                  _contract(max_reply_chars)):
        if block:
            parts.append(block)
    return "\n\n".join(p for p in parts if p)


def build_agent_user_message(context: dict) -> str:
    """当前事件 + 一句任务指令（提示词的最后一段用户消息）。"""
    ctx = context or {}
    kind = _EVENT_LABEL.get(str(ctx.get("type") or "chat"), "事件")
    return (f"【当前事件】{kind}\n{describe_event(ctx)}\n\n"
            "请判断这条事件要不要回、怎么回，并按输出要求给出 JSON。")
