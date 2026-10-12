"""礼物/欢迎/口播本地模板话术（PROMPTS 步骤 3.3，DESIGN §4.3；PRD FR-2/FR-3/FR-4）。

礼物与口播不走 LLM（DESIGN §4.3 的成本决定）：感谢/欢迎用本地模板随机变体，
口播直接读 config 的 schedule.pool。模板文案以代码常量承载——PRD FR-8 的
配置范围不含模板，后续要换文案可整体迁移至配置文件（本版本不迁移）。
称呼观众统一用昵称前 nickname_prefix_len 个字（与 chat 回复的人设规则一致）。
"""
import random as _random

from hub.logger import get_logger

# 礼物三档感谢（PRD FR-2 分档表：low 一句话带过 / mid 点名+两句话 / high 点名+热情+粉丝团引导）
GIFT_TEMPLATES = {
    "low": [
        "谢谢{user}的{gift}哈，老板大气",
        "收到{user}的{count}{gift}，谢啦兄弟",
        "{user}老板谢谢哈，兄弟们点点关注",
        "感谢{user}送的{gift}，么么哒",
    ],
    "mid": [
        "感谢{user}老板送的{count}{gift}！兄弟们把赞点起来",
        "哇谢谢{user}的{count}{gift}！老板大气，兄弟们学一学",
        "{user}老板太给力了，{count}{gift}收到！谢谢哈",
        "感谢{user}的{count}{gift}！这波必须给老板点个赞",
    ],
    "high": [
        "哇{user}老板{count}{gift}直接拉满！兄弟们粉丝团灯牌点起来，谢谢老板",
        "感谢{user}老板{count}{gift}冲榜！太热情了，粉丝团走一波",
        "{user}老板这{count}{gift}太顶了！跟着老板点个粉丝团，感谢感谢",
        "大礼物来了！谢谢{user}老板的{count}{gift}，兄弟们灯牌点起来",
    ],
}

# 欢迎（PRD FR-3：顺势引导关注/灯牌；enter 默认关闭，仅留开关用模板）
WELCOME_TEMPLATES = {
    "follow": [
        "欢迎{user}进直播间，点个关注不迷路",
        "{user}来了哈，兄弟们点点关注",
        "欢迎{user}，想玩同款点点小手柄",
    ],
    "fansclub": [
        "欢迎{user}进粉丝团，灯牌点起来",
        "{user}加入粉丝团了，谢谢老板支持",
        "感谢{user}上灯牌，兄弟们也点一个",
    ],
    "enter": [
        "欢迎{user}来到直播间",
        "{user}来了，坐哈",
        "欢迎{user}，点点关注哈",
    ],
}


def _shorten_name(user: str, prefix_len: int) -> str:
    user = (user or "").strip()
    if prefix_len <= 0 or not user:
        return user
    return user[:prefix_len]


def _count_text(count) -> str:
    n = max(int(count or 1), 1)
    return "一个" if n == 1 else f"{n}个"


class TemplateEngine:
    """sync (source, fields) -> str，Router 的 template_renderer 注入点。"""

    def __init__(self, config: dict = None, random=None, log=None):
        cfg = config or {}
        self._prefix_len = int((cfg.get("persona") or {}).get("nickname_prefix_len", 2))
        pool = (cfg.get("schedule") or {}).get("pool") or []
        self._pool = [str(x).strip() for x in pool if str(x).strip()]
        self._random = random or _random
        self._log = log or get_logger("templates")
        self._last_pool_idx = -1
        # v3.1 话术存档事件模板覆盖（gift_low/mid/high、follow/fansclub/enter）；
        # 键缺/空列表时回落代码常量默认文案
        self._overrides: dict = {}

    def set_event_templates(self, mapping: dict | None) -> None:
        """热替换事件模板（话术存档切换/保存后调用；None 恢复代码默认文案）。"""
        self._overrides = dict(mapping or {})
        self._log.info("event_templates_swapped", {"keys": sorted(self._overrides)})

    def _templates_for(self, key: str, fallback: list) -> list:
        items = self._overrides.get(key)
        if isinstance(items, (list, tuple)):
            items = [str(x).strip() for x in items if str(x or "").strip()]
            if items:
                return items
        return fallback

    def render(self, source: str, fields: dict) -> str:
        fields = fields or {}
        if source == "gift":
            tier = str(fields.get("tier", "low"))
            fallback = GIFT_TEMPLATES.get(tier) or GIFT_TEMPLATES["low"]
            return self._format(self._choice(
                self._templates_for(f"gift_{tier}", fallback)), fields)
        if source in WELCOME_TEMPLATES:
            return self._format(self._choice(
                self._templates_for(source, WELCOME_TEMPLATES[source])), fields)
        if source == "schedule":
            return self._pick_pool()
        self._log.warning("template_unknown_source", {"source": source})
        return ""

    def _choice(self, templates: list) -> str:
        return templates[self._random.randrange(len(templates))]

    def _format(self, template: str, fields: dict) -> str:
        values = {
            "user": _shorten_name(str(fields.get("user", "")), self._prefix_len),
            "gift": str(fields.get("gift", "")),
            "count": _count_text(fields.get("count", 1)),
        }
        try:
            return template.format(**values)
        except (KeyError, IndexError) as e:
            self._log.warning("template_format_error", {"template": template, "error": str(e)})
            return template

    def _pick_pool(self) -> str:
        """口播话术（PRD FR-4）：话术池随机轮换，连续两条不重复。"""
        if not self._pool:
            self._log.warning("schedule_pool_empty")
            return ""
        if len(self._pool) == 1:
            return self._pool[0]
        idx = self._random.randrange(len(self._pool))
        if idx == self._last_pool_idx:
            idx = (idx + 1) % len(self._pool)
        self._last_pool_idx = idx
        return self._pool[idx]
