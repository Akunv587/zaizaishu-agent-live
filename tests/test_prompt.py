"""prompt 构建与清洗的单元测试（PROMPTS 步骤 3.2）。

覆盖：system 提示词模板全变量渲染、user message 聚合渲染、
clean_reply 的引号剥离 / emoji 删除 / 超长截断三类用例。
"""
from hub.prompt import build_system_prompt, build_user_message, clean_reply


CFG = {
    "persona": {
        "nickname": "仔仔鼠", "style": "口语化、热情、短句、爱说兄弟们和哈哈",
        "max_reply_chars": 40,
    },
    "game": {
        "name": "《示例游戏》", "intro": "一句话玩法亮点",
        "download_hint": "点点小手柄就能下载",
        "highlights": ["卖点一", "卖点二", "卖点三"],
    },
}


def test_system_prompt_renders_all_variables():
    prompt = build_system_prompt(CFG)
    assert "仔仔鼠" in prompt
    assert "口语化、热情、短句、爱说兄弟们和哈哈" in prompt
    assert "不超过40字" in prompt
    assert "《示例游戏》" in prompt
    assert "一句话玩法亮点" in prompt
    assert "点点小手柄就能下载" in prompt
    assert "卖点一、卖点二、卖点三" in prompt


def test_system_prompt_keeps_design_structure():
    """结构逐行取自 DESIGN §4.3：标题行、知识库四行、行为规则四条。"""
    prompt = build_system_prompt(CFG)
    assert prompt.startswith("你是抖音游戏主播「仔仔鼠」，正在直播《示例游戏》")
    lines = prompt.splitlines()
    assert "知识库：" in lines
    assert "行为规则：" in lines
    assert any(line.startswith("- 游戏：") for line in lines)
    assert any(line.startswith("- 下载引导：") for line in lines)
    assert any(line.startswith("- 直播看点：") for line in lines)
    assert "一会儿评论区置顶告诉你" in prompt


def test_system_prompt_defaults_when_config_empty():
    prompt = build_system_prompt({})
    assert "「主播」" in prompt
    assert "不超过40字" in prompt


def test_user_message_single_member():
    ctx = {"user": "甲", "content": "这是什么游戏",
           "members": [("甲", "这是什么游戏")]}
    assert build_user_message(ctx) == "甲：这是什么游戏"


def test_user_message_merged_group_lines():
    ctx = {"user": "甲", "content": "这游戏好玩吗", "merged_count": 3,
           "members": [("甲", "这游戏好玩吗"), ("乙", "这游戏好玩吗？"), ("丙", "好玩吗这游戏")]}
    assert build_user_message(ctx) == "甲：这游戏好玩吗\n乙：这游戏好玩吗？\n丙：好玩吗这游戏"


def test_user_message_falls_back_to_user_content():
    ctx = {"user": "甲", "content": "单条弹幕", "members": []}
    assert build_user_message(ctx) == "甲：单条弹幕"


def test_clean_reply_strips_quotes():
    assert clean_reply("「哈哈这游戏好玩」") == "哈哈这游戏好玩"
    assert clean_reply("  “兄弟们上号！”  ") == "兄弟们上号！"
    assert clean_reply("'点点关注'") == "点点关注"


def test_clean_reply_removes_emoji():
    assert clean_reply("哈哈😂这游戏真好玩") == "哈哈这游戏真好玩"
    assert clean_reply("兄弟们⭐点点关注🎉🎉") == "兄弟们点点关注"


def test_clean_reply_empty_after_emoji_only():
    assert clean_reply("🎉😂⭐") == ""


def test_clean_reply_truncates_at_last_punctuation():
    raw = "哈哈这游戏真的特别好玩兄弟们，快点小手柄下载，跟着我一起上分冲起来"
    result = clean_reply(raw, max_chars=20)
    assert len(result) <= 20
    assert result == "哈哈这游戏真的特别好玩兄弟们，"


def test_clean_reply_hard_cut_without_punctuation():
    raw = "一二三四五六七八九十一二三四五六七八九十一二三四五"
    result = clean_reply(raw, max_chars=10)
    assert result == "一二三四五六七八九十"


def test_clean_reply_keeps_short_text():
    assert clean_reply("哈哈这游戏好玩哈") == "哈哈这游戏好玩哈"


def test_clean_reply_none_and_empty():
    assert clean_reply("") == ""
    assert clean_reply(None) == ""
