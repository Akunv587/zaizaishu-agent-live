"""人设提示词构建与输出清洗（PROMPTS 步骤 3.2，DESIGN §4.3；PRD FR-6/FR-8）。

模板结构逐行取自 DESIGN §4.3，变量全部来自 config 的 persona 与 game 组。
clean_reply 负责把 LLM 输出修剪成可口播的短句：剥引号、删 emoji、超长截断。
"""
import re

_TEMPLATE = """你是抖音游戏主播「{nickname}」，正在直播《{game_name}》，边玩边和观众聊天。
风格：{style}。每条话术不超过{max_reply_chars}字，只输出口播内容本身，
不要旁白、引号、表情符号。
知识库：
- 游戏：{game_name}，{game_intro}
- 下载引导：{download_hint}
- 直播看点：{highlights}
行为规则：
- 称呼观众用其昵称前两个字
- 只聊这款游戏和直播间话题，其他话题礼貌带过
- 遇到挑衅、引战不接茬，笑着带过或忽略
- 回答游戏相关问题只用知识库信息，知识库没有的就说"一会儿评论区置顶告诉你\""""

# 首尾要剥离的引号与空白（ASCII 与全角引号、直角引号）
_QUOTES = "\"'“”‘’「」『』 \t\r\n"

# 常见 emoji 区块：杂项技术符号中的表情（⌚⏰）、杂项符号与装饰符号（☀✂❤）、
# 星号箭头区（⭐✨）、变体选择符、零宽连接符、表情象形主区（含旗帜指示符）
_EMOJI_RE = re.compile(
    "["
    "\U0000231A-\U000023FF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "\U0001F000-\U0001FAFF"
    "]+"
)


def build_system_prompt(cfg: dict) -> str:
    """渲染 DESIGN §4.3 的系统提示词模板，全部变量取自 config。"""
    cfg = cfg or {}
    persona = cfg.get("persona") or {}
    game = cfg.get("game") or {}
    highlights = game.get("highlights") or []
    if isinstance(highlights, str):
        highlights = [highlights]
    game_name = str(game.get("name", "") or "").strip() or "本款游戏"
    # 模板自带书名号（正在直播《{game_name}》），配置值若也带《》会双重包裹，此处归一
    while game_name.startswith("《") and game_name.endswith("》") and len(game_name) > 2:
        game_name = game_name[1:-1].strip() or "本款游戏"
    return _TEMPLATE.format(
        nickname=persona.get("nickname", "主播"),
        style=persona.get("style", "口语化、热情、短句"),
        max_reply_chars=persona.get("max_reply_chars", 40),
        game_name=game_name,
        game_intro=game.get("intro", ""),
        download_hint=game.get("download_hint", ""),
        highlights="、".join(str(h) for h in highlights),
    )


def build_user_message(context: dict) -> str:
    """聚合组渲染为逐行「观众昵称：内容」，引导模型一条回复覆盖整组。"""
    members = (context or {}).get("members") or []
    if not members:
        members = [(context.get("user", ""), context.get("content", ""))]
    return "\n".join(f"{user}：{content}" for user, content in members)


def clean_reply(raw: str, max_chars: int = 40) -> str:
    """剥离首尾引号与空白、删除常见 emoji；超长时在最后一个句号/逗号处截断。"""
    if raw is None:
        return ""
    text = str(raw).strip().strip(_QUOTES).strip()
    text = _EMOJI_RE.sub("", text)
    text = text.strip().strip(_QUOTES).strip()
    if max_chars and len(text) > max_chars:
        window = text[:max_chars]
        cut = max(window.rfind("。"), window.rfind("，"))
        text = window[:cut + 1] if cut >= 0 else window
    return text
