"""智能体内核单元测试：单场记忆、情绪校验、决策契约（M4-1，DESIGN §5.3/§8.6/§10.1）。

验收口径（V4.2~V4.5、V4.7 的单元层）：
- 记忆环形缓冲满容量后挤掉最旧的，顺序正确（T7）；
- 防复读按相似度判定，不是精确比对；
- 情绪三种处置（档位内 / 8 枚举内 / 非法）与两个开关的边界；
- 决策契约四种非法输出的处置，且**绝不把 JSON 残骸当话术念出来**（V4.2）；
- `should_reply=false` 时下游拿不到任何可播文本（V4.3）。
"""
import pytest

from hub.agent.decide import (
    DEGRADE_EXTRACT,
    DEGRADE_PLAIN,
    DEGRADE_SALVAGE,
    AgentDecision,
    DecisionError,
    parse_decision,
)
from hub.agent.emotion import (
    EVENT_INVALID,
    EVENT_ONSITE,
    EVENT_ONSITE_BLOCKED,
    EVENT_SPEED_CLAMPED,
    emotion_hint,
    resolve_emotion,
    resolve_speed,
)
from hub.agent.memory import SessionMemory
from hub.config import TTS_EMOTIONS
from hub.events import LiveEvent


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


# ---------------------------------------------------------------- 单场记忆


def test_memory_ring_buffer_evicts_oldest_and_keeps_order():
    """T7：满容量后新事件挤掉最旧的，读取顺序为时间正序。"""
    mem = SessionMemory(max_events=3)
    for i in range(5):
        mem.add_event(LiveEvent(type="chat", user=f"u{i}", content=f"第{i}条",
                                ts=1000 + i))
    events = mem.events()
    assert [e["user"] for e in events] == ["u2", "u3", "u4"]
    assert mem.stats()["events"] == 3


def test_memory_event_summary_truncates_long_content():
    mem = SessionMemory(max_events=5)
    mem.add_event(LiveEvent(type="chat", user="阿强", content="喂" * 500, ts=1.0))
    summary = mem.events()[0]
    assert len(summary["content"]) == 121          # 120 字 + 省略号
    assert summary["content"].endswith("…")


def test_memory_event_lines_render_gift_and_chat():
    mem = SessionMemory(max_events=5)
    mem.add_event(LiveEvent(type="chat", user="阿强", content="这游戏怎么玩", ts=3600))
    mem.add_event(LiveEvent(type="gift", user="小明", gift_name="小心心",
                            gift_count=5, gift_value=10, ts=3601))
    mem.add_event(LiveEvent(type="follow", user="小红", ts=3602))
    lines = mem.event_lines()
    assert lines[0].endswith("弹幕 阿强：这游戏怎么玩")
    assert "礼物 小明 送出 小心心 ×5（价值 10）" in lines[1]
    assert lines[2].endswith("关注 小红")


def test_memory_event_lines_tolerate_missing_timestamp():
    mem = SessionMemory()
    mem.add_event(LiveEvent(type="chat", user="阿强", content="在吗", ts=0.0))
    assert mem.event_lines()[0].startswith("[--:--:--]")


def test_memory_spoken_dedup_by_similarity_not_exact():
    """防复读按相似度判定：措辞略变也算重复（不是精确比对）。"""
    mem = SessionMemory(max_spoken=10)
    mem.add_spoken("兄弟这个游戏怎么玩呀")
    assert mem.is_repeated("兄弟这个游戏怎么玩呀") is True
    assert mem.is_repeated("兄弟这个游戏怎么玩") is True    # 相似度 8/9
    assert mem.is_repeated("主播你唱歌真好听") is False
    assert mem.is_repeated("") is False


def test_memory_spoken_ring_evicts_oldest():
    mem = SessionMemory(max_spoken=2)
    mem.add_spoken("第一句")
    mem.add_spoken("第二句")
    mem.add_spoken("第三句")
    assert mem.spoken() == ["第二句", "第三句"]


def test_memory_zero_notes_is_valid_and_drops_writes():
    """max_notes=0 是 config 的合法下界——写入即丢弃，不报错。"""
    mem = SessionMemory(max_notes=0)
    mem.add_note("刚答过下载问题")
    assert mem.notes() == []
    assert mem.stats()["notes"] == 0


def test_memory_notes_dedup_and_clear():
    mem = SessionMemory(max_notes=3)
    mem.add_note("刚答过下载问题")
    mem.add_note("刚答过下载问题")
    mem.add_note("  ")
    assert mem.notes() == ["刚答过下载问题"]
    mem.clear()
    assert mem.brief() == {"events": [], "spoken": [], "notes": []}


# ---------------------------------------------------------------- 情绪校验


def test_emotion_in_profile_set_is_used_as_is():
    emo, event = resolve_emotion("happy", ["default", "happy", "calm"])
    assert (emo, event) == ("happy", "")


def test_emotion_in_enum_but_not_in_profile_goes_onsite():
    emo, event = resolve_emotion("sad", ["default", "happy"])
    assert (emo, event) == ("sad", EVENT_ONSITE)


def test_emotion_invalid_falls_back_to_default():
    for bad in ("angryy", "不知道", "DEFAULT?", None, ""):
        emo, event = resolve_emotion(bad, ["default", "happy"])
        assert (emo, event) == ("default", EVENT_INVALID), bad


def test_emotion_default_always_resolvable_even_with_empty_profile():
    """档位集合为空时 default 仍可用（它永远可播）。"""
    assert resolve_emotion("default", []) == ("default", "")
    assert resolve_emotion("default", None) == ("default", "")


def test_emotion_disabled_switch_forces_default():
    emo, event = resolve_emotion("happy", ["default", "happy"], enabled=False)
    assert (emo, event) == ("default", "")


def test_emotion_onsite_fallback_off_falls_back_to_prerecorded():
    emo, event = resolve_emotion("sad", ["default", "happy"], onsite_fallback=False)
    assert (emo, event) == ("default", EVENT_ONSITE_BLOCKED)


def test_emotion_case_and_space_insensitive():
    assert resolve_emotion(" Happy ", ["happy"]) == ("happy", "")


def test_emotion_hint_lists_profile_set_in_enum_order():
    hint = emotion_hint(["default", "calm", "happy"])
    assert "可用情绪档位：default, happy, calm" in hint
    assert "现场合成" in hint
    assert "将被忽略" in emotion_hint(["default"], onsite_fallback=False)


def test_resolve_speed_clamps_and_falls_back():
    assert resolve_speed(None) == (None, "")
    assert resolve_speed("abc") == (None, "")
    assert resolve_speed("1.5") == (1.5, "")
    assert resolve_speed(3.0) == (2.0, EVENT_SPEED_CLAMPED)
    assert resolve_speed(0.1) == (0.5, EVENT_SPEED_CLAMPED)
    assert resolve_speed(float("nan"), fallback=1.2) == (1.2, "")
    assert resolve_speed(None, fallback=1.2) == (1.2, "")


# ---------------------------------------------------------------- 决策契约

_GOOD = ('{"should_reply": true, "priority": 0, "interrupt": true, "tone": "热情", '
         '"emotion": "happy", "speed": 1.05, "reason": "有效提问", '
         '"reply_text": "兄弟这个游戏点小手柄下载就行", "memory_note": "答过下载"}')


def test_parse_full_json_keeps_every_field():
    d = parse_decision(_GOOD)
    assert d.should_reply is True
    assert d.priority == 0
    assert d.interrupt is True
    assert d.tone == "热情"
    assert d.emotion == "happy"
    assert d.speed == 1.05
    assert d.reason == "有效提问"
    assert d.reply_text == "兄弟这个游戏点小手柄下载就行"
    assert d.memory_note == "答过下载"
    assert d.degrade == ""
    assert d.has_reply() is True


def test_parse_json_fenced_block():
    d = parse_decision(f"```json\n{_GOOD}\n```")
    assert d.reply_text == "兄弟这个游戏点小手柄下载就行"
    assert d.degrade == ""


def test_parse_json_surrounded_by_prose_extracts_balanced_block():
    """混入说明文字（V4.2 情形二）：括号配平提取，不把散文圈进去。"""
    d = parse_decision(f"好的，我的判断如下：\n{_GOOD}\n以上是我这一轮的回复。")
    assert d.degrade == DEGRADE_EXTRACT
    assert d.reply_text == "兄弟这个游戏点小手柄下载就行"
    assert d.priority == 0


def test_parse_truncated_json_salvages_reply_text():
    """截断（V4.2 情形一）：抢救出 reply_text，绝不把 JSON 残骸念出去。"""
    d = parse_decision('{"should_reply": true, "priority": 1, '
                       '"reply_text": "哥们这个游戏挂着就行", "reason": "值得回')
    assert d.degrade == DEGRADE_SALVAGE
    assert d.reply_text == "哥们这个游戏挂着就行"
    assert "should_reply" not in d.reply_text
    assert d.priority == 1


def test_parse_truncated_json_with_should_reply_false_stays_silent():
    d = parse_decision('{"should_reply": false, "reason": "这条不值得回，因为')
    assert d.should_reply is False
    assert d.reply_text == ""
    assert d.has_reply() is False


def test_parse_truncated_json_without_salvage_raises():
    """残骸且抢救不出来 → 抛错走兜底口播，而不是把 `{"should_reply":` 念出去。"""
    with pytest.raises(DecisionError):
        parse_decision('{"should_reply": true, "priority"')


def test_parse_plain_prose_becomes_reply_text():
    """完全非 JSON（V4.2 情形三）：纯文本当话术，priority=1，reason=解析降级。"""
    d = parse_decision("哎呀这游戏就是点小手柄下载，进去挂着就行")
    assert d.degrade == DEGRADE_PLAIN
    assert d.priority == 1
    assert d.reason == "解析降级"
    assert d.should_reply is True
    assert d.reply_text == "哎呀这游戏就是点小手柄下载，进去挂着就行"


def test_parse_empty_response_raises():
    for empty in ("", "   ", None):
        with pytest.raises(DecisionError):
            parse_decision(empty)


def test_emotion_only_prose_raises():
    with pytest.raises(DecisionError):
        parse_decision("🙂🙂🙂")


def test_should_reply_false_clears_reply_text():
    """V4.3：主动不回时下游拿不到任何可播文本。"""
    d = parse_decision('{"should_reply": false, "reply_text": "这条别播"}')
    assert d.should_reply is False
    assert d.reply_text == ""
    assert d.has_reply() is False


def test_missing_should_reply_derives_from_reply_text():
    assert parse_decision('{"reply_text": "在的兄弟"}').should_reply is True
    assert parse_decision('{"priority": 1}').should_reply is False


def test_priority_and_interrupt_are_clamped():
    d = parse_decision('{"priority": 9, "interrupt": true, "reply_text": "x"}')
    assert d.priority == 2
    assert d.interrupt is False          # 非 0 优先级不允许插队
    d2 = parse_decision('{"priority": "0", "interrupt": "yes", "reply_text": "x"}')
    assert (d2.priority, d2.interrupt) == (0, True)
    d3 = parse_decision('{"priority": "abc", "reply_text": "x"}')
    assert d3.priority == 1


def test_reply_text_is_cleaned_and_truncated():
    d = parse_decision('{"reply_text": "“他说：你好呀🙂”"}', max_reply_chars=4)
    assert "🙂" not in d.reply_text
    assert len(d.reply_text) <= 4


def test_decision_record_is_json_serializable():
    """V4.5 的单元层：决策能原样落进 JSONL。"""
    import json
    record = parse_decision(_GOOD).to_record()
    assert json.loads(json.dumps(record, ensure_ascii=False))["emotion"] == "happy"
    assert json.loads(json.dumps(AgentDecision().to_record()))["should_reply"] is False


def test_all_minimax_emotions_are_accepted_by_resolver():
    """8 枚举 + default 全能过校验，不出现"合法情绪被判非法"。"""
    for e in TTS_EMOTIONS:
        assert resolve_emotion(e, ["default"])[1] == EVENT_ONSITE
        assert resolve_emotion(e, ["default", e]) == (e, "")
