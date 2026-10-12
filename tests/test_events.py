"""hub.events 归一化的单元测试（PROMPTS 步骤 1.3）。

fixture 为符合 Dycast DyMessage 结构的合成消息；待步骤 1.1 录得真实样本
replay/sample-001.jsonl 后，以真实行替换扩充（PROMPTS 验收要求）。
"""
import pytest

from hub.events import LiveEvent, SpeakTask, normalize


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "info")
    return tmp_path


def test_chat_normalize():
    ev = normalize({"method": "WebcastChatMessage", "user": {"name": "张三"}, "content": "主播这是什么游戏"}, ts=100.0)
    assert ev is not None
    assert ev.type == "chat"
    assert ev.user == "张三"
    assert ev.content == "主播这是什么游戏"
    assert ev.ts == 100.0
    assert ev.gift_count == 1 and ev.gift_value == 0 and ev.combo_end is False


def test_gift_normalize_int_fields():
    ev = normalize({
        "method": "WebcastGiftMessage",
        "user": {"name": "李四"},
        "gift": {"name": "小心意", "count": 3, "price": 100, "repeatEnd": True},
    }, ts=200.0)
    assert ev.type == "gift"
    assert ev.gift_name == "小心意"
    assert ev.gift_count == 3
    assert ev.gift_value == 100  # OQ-1：单位口径待 2.4 确认，此处只透传
    assert ev.combo_end is True


def test_gift_count_str_and_repeat_end_variants():
    ev = normalize({
        "method": "WebcastGiftMessage",
        "user": {"name": "王五"},
        "gift": {"name": "大火箭", "count": "7", "price": "500", "repeatEnd": 1},
    }, ts=300.0)
    assert ev.gift_count == 7  # count 容错 str
    assert ev.gift_value == 500  # price 容错 str
    assert ev.combo_end is True  # repeatEnd 容错 int

    ev2 = normalize({
        "method": "WebcastGiftMessage",
        "user": {"name": "王五"},
        "gift": {"name": "大火箭", "count": 2, "price": 0, "repeatEnd": 0},
    }, ts=300.0)
    assert ev2.combo_end is False


def test_follow_normalize():
    ev = normalize({"method": "WebcastSocialMessage", "user": {"name": "赵六"}}, ts=400.0)
    assert ev.type == "follow"
    assert ev.user == "赵六"
    assert ev.content == ""


def test_fansclub_normalize_with_content():
    ev = normalize({"method": "WebcastFansclubMessage", "user": {"name": "钱七"}, "content": "加入粉丝团"}, ts=500.0)
    assert ev.type == "fansclub"
    assert ev.content == "加入粉丝团"


def test_enter_normalize():
    ev = normalize({"method": "WebcastMemberMessage", "user": {"name": "孙八"}}, ts=600.0)
    assert ev.type == "enter"


def test_normalize_carries_test_flag():
    ev = normalize({"method": "WebcastChatMessage", "user": {"name": "测试"},
                    "content": "你好", "test": True}, ts=1.0)
    assert ev.test is True
    assert normalize({"method": "WebcastChatMessage", "user": {"name": "测试"},
                      "content": "你好"}, ts=1.0).test is False


def test_missing_user_returns_none():
    assert normalize({"method": "WebcastChatMessage", "content": "没人"}, ts=1.0) is None
    assert normalize({"method": "WebcastGiftMessage", "gift": {"name": "礼物"}}, ts=1.0) is None


def test_missing_content_returns_none():
    assert normalize({"method": "WebcastChatMessage", "user": {"name": "张三"}}, ts=1.0) is None
    assert normalize({"method": "WebcastChatMessage", "user": {"name": "张三"}, "content": ""}, ts=1.0) is None


def test_missing_gift_fields_return_none():
    assert normalize({"method": "WebcastGiftMessage", "user": {"name": "李四"}}, ts=1.0) is None
    assert normalize({"method": "WebcastGiftMessage", "user": {"name": "李四"}, "gift": {"count": 1}}, ts=1.0) is None


def test_non_whitelist_and_invalid_input_return_none():
    assert normalize({"method": "WebcastLikeMessage", "user": {"name": "x"}}, ts=1.0) is None
    assert normalize({"method": "别的"}, ts=1.0) is None
    assert normalize({}, ts=1.0) is None
    assert normalize(None, ts=1.0) is None
    assert normalize([1, 2], ts=1.0) is None


def test_speak_task_defaults():
    task = SpeakTask(priority=1, text="你好", event_ts=100.0, source="chat")
    assert task.merged_count == 1
    assert task.tier == ""
    assert task.interrupt is False


def test_timestamp_default_uses_now():
    ev = normalize({"method": "WebcastSocialMessage", "user": {"name": "即时"}}, ts=None)
    assert ev.ts > 0
