"""本地模板话术引擎的单元测试（PROMPTS 步骤 3.3）。

随机源注入确定 picker：randrange(n) 的返回值可控，验证
占位符替换、三档分派、变体边界不越界、口播池不连续重复。
"""
from hub.templates import GIFT_TEMPLATES, WELCOME_TEMPLATES, TemplateEngine


class FakeRandom:
    def __init__(self, picker):
        self._picker = picker

    def randrange(self, n):
        return self._picker(n)


def expected(template: str, fields: dict, prefix_len: int = 2) -> str:
    user = str(fields["user"])[:prefix_len]
    n = max(int(fields.get("count", 1) or 1), 1)
    count = "一个" if n == 1 else f"{n}个"
    return template.format(user=user, gift=fields.get("gift", ""), count=count)


CFG = {"persona": {"nickname_prefix_len": 2},
       "schedule": {"pool": ["口播一", "口播二", "口播三"]}}


def make_engine(picker, cfg=None):
    return TemplateEngine(config=cfg or CFG, random=FakeRandom(picker))


def test_gift_placeholder_substitution_all_three():
    engine = make_engine(lambda n: 0)
    fields = {"user": "小明同学", "gift": "小心意", "count": 3, "tier": "mid"}
    result = engine.render("gift", fields)
    assert result == expected(GIFT_TEMPLATES["mid"][0], fields)
    assert "{" not in result and "}" not in result
    assert "小明" in result
    assert "小心意" in result
    assert "3个" in result


def test_gift_three_tiers_pick_own_template_set():
    for tier in ("low", "mid", "high"):
        engine = make_engine(lambda n, t=tier: 1)
        fields = {"user": "榜一大哥", "gift": "嘉年华", "count": 2, "tier": tier}
        result = engine.render("gift", fields)
        assert result == expected(GIFT_TEMPLATES[tier][1], fields)


def test_gift_unknown_tier_falls_back_to_low():
    engine = make_engine(lambda n: 0)
    fields = {"user": "观众", "gift": "小心意", "count": 1, "tier": "奇怪档"}
    result = engine.render("gift", fields)
    assert result == expected(GIFT_TEMPLATES["low"][0], fields)


def test_variant_bounds_first_and_last():
    fields = {"user": "小明同学", "gift": "玫瑰", "count": 2, "tier": "high"}
    first = make_engine(lambda n: 0).render("gift", fields)
    last = make_engine(lambda n: n - 1).render("gift", fields)
    assert first == expected(GIFT_TEMPLATES["high"][0], fields)
    assert last == expected(GIFT_TEMPLATES["high"][-1], fields)


def test_gift_count_one_renders_geci_word():
    engine = make_engine(lambda n: 1)
    fields = {"user": "乙", "gift": "小心意", "count": 1, "tier": "low"}
    result = engine.render("gift", fields)
    assert result == expected(GIFT_TEMPLATES["low"][1], fields)
    assert "一个小心意" in result


def test_welcome_follow_and_fansclub():
    for kind in ("follow", "fansclub", "enter"):
        engine = make_engine(lambda n, k=kind: 2)
        result = engine.render(kind, {"user": "张三丰"})
        assert result == expected(WELCOME_TEMPLATES[kind][2], {"user": "张三丰"})
        assert "张三" in result


def test_short_user_not_cut():
    engine = make_engine(lambda n: 0)
    result = engine.render("follow", {"user": "甲"})
    assert "甲" in result and "「" not in result


def test_schedule_pool_no_immediate_repeat():
    engine = make_engine(lambda n: 1)  # 恒返回索引 1，第二条应顺延到 2
    first = engine.render("schedule", {})
    second = engine.render("schedule", {})
    assert first == "口播二"
    assert second == "口播三"


def test_schedule_pool_single_item():
    cfg = {"schedule": {"pool": ["只有一条"]}}
    engine = make_engine(lambda n: 0, cfg=cfg)
    assert engine.render("schedule", {}) == "只有一条"


def test_schedule_pool_empty_returns_empty():
    engine = make_engine(lambda n: 0, cfg={"schedule": {}})
    assert engine.render("schedule", {}) == ""


def test_unknown_source_returns_empty():
    engine = make_engine(lambda n: 0)
    assert engine.render("mystery", {"user": "甲"}) == ""


# ---------- v3.1：话术存档事件模板覆盖 ----------

def test_overrides_replace_gift_templates():
    engine = make_engine(lambda n: 0)
    engine.set_event_templates({"gift_mid": ["自定义感谢{user}的{count}{gift}"]})
    result = engine.render("gift", {"user": "小明同学", "gift": "玫瑰", "count": 2,
                                    "tier": "mid"})
    assert result == "自定义感谢小明的2个玫瑰"


def test_overrides_partial_keys_fall_back_to_defaults():
    engine = make_engine(lambda n: 0)
    engine.set_event_templates({"gift_low": ["低档自定义{user}"]})
    # 覆盖的键用覆盖文案
    assert engine.render("gift", {"user": "甲乙", "tier": "low"}) == "低档自定义甲乙"
    # 未覆盖的键回落代码默认
    result = engine.render("gift", {"user": "甲乙", "gift": "玫瑰", "count": 1,
                                    "tier": "high"})
    assert result == expected(GIFT_TEMPLATES["high"][0], {"user": "甲乙", "gift": "玫瑰",
                                                          "count": 1})


def test_overrides_empty_list_falls_back_and_none_clears():
    engine = make_engine(lambda n: 0)
    engine.set_event_templates({"follow": []})
    assert engine.render("follow", {"user": "张三丰"}) == \
        expected(WELCOME_TEMPLATES["follow"][0], {"user": "张三丰"})
    engine.set_event_templates({"follow": ["覆盖欢迎{user}"]})
    assert engine.render("follow", {"user": "张三丰"}) == "覆盖欢迎张三"
    engine.set_event_templates(None)  # 清除覆盖恢复默认
    assert engine.render("follow", {"user": "张三丰"}) == \
        expected(WELCOME_TEMPLATES["follow"][0], {"user": "张三丰"})


def test_overrides_welcome_kinds():
    engine = make_engine(lambda n: 0)
    engine.set_event_templates({"fansclub": ["粉丝团自定义{user}"],
                                "enter": ["进场自定义{user}"]})
    assert engine.render("fansclub", {"user": "榜一"}) == "粉丝团自定义榜一"
    assert engine.render("enter", {"user": "榜一"}) == "进场自定义榜一"
