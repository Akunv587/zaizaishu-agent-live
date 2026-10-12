"""`AgentBrain` 决策主流程的单元测试（M4-3）。

验收口径（V4.1、V4.3~V4.7 的主流程层）：
- 每条事件都拿到字段齐全的决策（V4.1）；
- `should_reply: false` 时不返回任何可播内容（V4.3）；
- 情绪三种处置在主流程里落地正确（V4.4）；
- 决策日志每行一个 JSON、字段齐全（V4.5）；
- 单场记忆生效：无论回不回都记事件，说过的话进防复读缓冲（V4.6）；
- 原样采纳召回素材时复用其预录音频（V4.7 的下游用途）。
"""
import asyncio
import json

import pytest

from hub.agent.brain import AgentBrain
from hub.agent.decide import DecisionError
from hub.agent.llm import AgentLLMError
from hub.agent.log import DecisionLogger

PROFILE = {
    "samples_ai": [
        {"id": "s1", "enabled": True, "type_name": "问下载",
         "examples": ["这游戏怎么下载", "在哪下载啊"],
         "reply_text": "右下角小手柄点一下就能下载"},
        {"id": "s2", "enabled": True, "type_name": "打招呼",
         "examples": ["主播好"], "reply_text": "兄弟好呀，欢迎来玩"},
    ],
    "topics_ai": [{"id": "t1", "enabled": True, "text": "这游戏主打休闲挂机"}],
}


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


class StubLLM:
    """按序返回预设响应（或抛预设异常），并记录每次的 system/user。"""

    def __init__(self, answers, model="stub-model"):
        self._answers = list(answers)
        self.model = model
        self.calls = []

    async def ask(self, system, user):
        self.calls.append({"system": system, "user": user})
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class StubSelector:
    def __init__(self, profile=None, emotions=("default", "happy"), audio=None,
                 max_reply_chars=30):
        self.profile = PROFILE if profile is None else profile
        self._emotions = list(emotions)
        self._audio = dict(audio or {})
        self._max_reply_chars = max_reply_chars
        self.audio_queries = []

    def build_system_prompt(self, base_cfg=None):
        return "你是抖音游戏主播「仔仔鼠」"

    def current_emotions(self):
        return list(self._emotions)

    def max_reply_chars(self, default=40):
        return self._max_reply_chars

    def resolve_audio(self, item, emotion=""):
        self.audio_queries.append((item.get("id"), emotion))
        return self._audio.get(item.get("id"), "")


def make_brain(tmp_path, answers, selector=None, settings=None, stats=None,
               examples=None, emotions=("default", "happy"), audio=None,
               log_enabled=True):
    return AgentBrain(
        config={"llm": {"base_url": "u", "api_key": "k", "model": "m"},
                "persona": {"max_reply_chars": 30}},
        settings=settings,
        selector=selector if selector is not None else StubSelector(
            emotions=emotions, audio=audio),
        llm=StubLLM(answers),
        decision_log=DecisionLogger(log_dir=tmp_path, enabled=log_enabled),
        examples=examples, stats=stats)


def chat(content="这游戏怎么下载", user="阿强"):
    return {"type": "chat", "user": user, "content": content,
            "merged_count": 1, "members": [(user, content)],
            "event_ts": 3600.0, "test": False}


GOOD = ('{"should_reply": true, "priority": 1, "interrupt": false, "tone": "热情", '
        '"emotion": "happy", "speed": 1.0, "reason": "新观众问下载", '
        '"reply_text": "兄弟点右下角小手柄就能下", "memory_note": "答过下载"}')


def test_decision_returns_full_contract_dict(tmp_path):
    brain = make_brain(tmp_path, [GOOD])
    result = asyncio.run(brain.handle(chat()))
    assert result == {"text": "兄弟点右下角小手柄就能下", "audio_path": "",
                      "emotion": "happy", "speed": 1.0, "priority": 1,
                      "interrupt": False, "tone": "热情", "reason": "新观众问下载"}


def test_system_prompt_and_user_message_carry_material_and_memory(tmp_path):
    brain = make_brain(tmp_path, [GOOD])
    asyncio.run(brain.handle(chat()))
    sent = brain._llm.calls[0]
    assert sent["system"].startswith("你是抖音游戏主播「仔仔鼠」")
    assert "【本轮素材召回】" in sent["system"]
    assert "【可用情绪档位】" in sent["system"]
    assert "可用情绪档位：default, happy" in sent["system"]
    assert "【输出要求】" in sent["system"]
    assert sent["user"].startswith("【当前事件】弹幕")
    assert "阿强：这游戏怎么下载" in sent["user"]


def test_should_reply_false_produces_nothing(tmp_path):
    """V4.3：明确不回时不产出任何任务，且不记入 spoken。"""
    brain = make_brain(tmp_path, ['{"should_reply": false, "reason": "无意义弹幕"}'])
    assert asyncio.run(brain.handle(chat("。。。"))) is None
    assert brain.memory.spoken() == []


def test_event_is_remembered_even_when_not_replied(tmp_path):
    """V4.6：不回的事件也要进记忆，否则看不出"这条刚被问过"。"""
    brain = make_brain(tmp_path, ['{"should_reply": false, "reason": "刚答过"}'])
    asyncio.run(brain.handle(chat("这游戏怎么下载")))
    assert [e["content"] for e in brain.memory.events()] == ["这游戏怎么下载"]


def test_gift_and_follow_events_are_memorised(tmp_path):
    brain = make_brain(tmp_path, ['{"should_reply": true, "reply_text": "谢谢兄弟"}',
                                  '{"should_reply": true, "reply_text": "欢迎新朋友"}'])
    asyncio.run(brain.handle({"type": "gift", "user": "小明", "gift_name": "小心心",
                              "gift_count": 5, "gift_value": 10, "event_ts": 1.0}))
    asyncio.run(brain.handle({"type": "follow", "user": "小红", "event_ts": 2.0}))
    events = brain.memory.events()
    assert [e["type"] for e in events] == ["gift", "follow"]
    assert events[0]["gift_name"] == "小心心"


def test_spoken_and_notes_are_written_back(tmp_path):
    brain = make_brain(tmp_path, [GOOD])
    asyncio.run(brain.handle(chat()))
    assert brain.memory.spoken() == ["兄弟点右下角小手柄就能下"]
    assert brain.memory.notes() == ["答过下载"]


def test_llm_failure_propagates_for_router_fallback(tmp_path):
    """末级容错在 Router：brain 不自己兜底，但必须把失败抛上去。"""
    brain = make_brain(tmp_path, [AgentLLMError("boom"), AgentLLMError("boom")])
    with pytest.raises(AgentLLMError):
        asyncio.run(brain.handle(chat()))


def test_unusable_output_raises_decision_error(tmp_path):
    brain = make_brain(tmp_path, ["", '{"should_reply": true, "reply'])
    for _ in range(2):
        with pytest.raises(DecisionError):
            asyncio.run(brain.handle(chat()))


def test_degraded_plain_text_still_produces_a_reply(tmp_path):
    brain = make_brain(tmp_path, ["哎呀点小手柄下载就行，挂着玩"])
    result = asyncio.run(brain.handle(chat()))
    assert result["text"] == "哎呀点小手柄下载就行，挂着玩"
    assert result["priority"] == 1


def test_emotion_onsite_when_tier_not_preregistered(tmp_path):
    """V4.4：8 枚举内但存档没勾 → 接受，走现场 TTS（emotion 透传下去）。"""
    brain = make_brain(tmp_path, ['{"should_reply": true, "emotion": "sad", '
                                  '"reply_text": "别难过兄弟"}'],
                       emotions=("default", "happy"))
    result = asyncio.run(brain.handle(chat()))
    assert result["emotion"] == "sad"


def test_emotion_invalid_falls_back_to_default(tmp_path):
    brain = make_brain(tmp_path, ['{"should_reply": true, "emotion": "得意", '
                                  '"reply_text": "嘿嘿"}'])
    assert asyncio.run(brain.handle(chat()))["emotion"] == "default"


def test_emotion_disabled_switch_flattens_everything(tmp_path):
    settings = {"emotion": {"enabled": False, "onsite_fallback": True},
                "decision_log": True, "memory": {}, "retrieve": {}}
    brain = make_brain(tmp_path, ['{"should_reply": true, "emotion": "happy", '
                                  '"reply_text": "嘿"}'], settings=settings)
    result = asyncio.run(brain.handle(chat()))
    assert result["emotion"] == "default"
    assert result["speed"] is None       # emotion 关掉时语速也不覆盖


def test_speed_out_of_range_is_clamped(tmp_path):
    brain = make_brain(tmp_path, ['{"should_reply": true, "speed": 9, '
                                  '"reply_text": "冲"}'])
    assert asyncio.run(brain.handle(chat()))["speed"] == 2.0


def test_prerecorded_audio_is_reused_when_reply_matches_recall(tmp_path):
    """V4.7 的下游用途：原样采纳召回素材 → 播它的预录音频，不再合成一遍。"""
    selector = StubSelector(audio={"s1": "C:/audio/s1.wav"})
    brain = make_brain(tmp_path, ['{"should_reply": true, "reply_text": "右下角小手柄点一下就能下载"}'],
                       selector=selector)
    result = asyncio.run(brain.handle(chat("这游戏怎么下载")))
    assert result["audio_path"] == "C:/audio/s1.wav"
    assert ("s1", "default") in selector.audio_queries


def test_prerecorded_audio_not_reused_for_fresh_text(tmp_path):
    selector = StubSelector(audio={"s1": "C:/audio/s1.wav"})
    brain = make_brain(tmp_path, ['{"should_reply": true, "reply_text": "完全是新写的一句口播"}'],
                       selector=selector)
    assert asyncio.run(brain.handle(chat("这游戏怎么下载")))["audio_path"] == ""
    # 检索层为 has_audio 标注会以 emotion="" 探一次；复用判定才会带具体情绪档
    assert ("s1", "default") not in selector.audio_queries


def test_decision_log_record_has_every_field(tmp_path):
    """V4.5：每行一个 JSON，字段齐全，模型情绪与落地情绪分开记。"""
    brain = make_brain(tmp_path, ['{"should_reply": true, "emotion": "sad", '
                                  '"reason": "安慰", "reply_text": "别急慢慢来"}'],
                       emotions=("default", "happy"))
    asyncio.run(brain.handle(chat("这游戏怎么下载")))
    lines = brain.decision_log.path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["type"] == "chat"
    assert record["user"] == "阿强"
    assert record["emotion_raw"] == "sad"
    assert record["emotion"] == "sad"
    assert record["emotion_event"] == "emotion_onsite"
    assert record["should_reply"] is True
    assert record["reply_text"] == "别急慢慢来"
    assert record["model"] == "stub-model"
    assert record["latency_ms"] >= 0
    assert "sample:s1" in record["recall"]
    assert record["prerecorded"] is False
    assert record["ts"]


def test_decision_log_disabled_writes_nothing(tmp_path):
    """`agent.decision_log: false` 时连文件都不建（brain 自行按设置构造日志器）。"""
    brain = AgentBrain(
        config={"llm": {"base_url": "u", "api_key": "k", "model": "m"}},
        settings={"decision_log": False, "memory": {}, "retrieve": {}, "emotion": {}},
        selector=StubSelector(),
        llm=StubLLM([GOOD]),
    )
    assert brain.decision_log.enabled is False
    asyncio.run(brain.handle(chat()))
    assert brain.decision_log.path() is None


def test_stats_counters(tmp_path):
    class Stats:
        def __init__(self):
            self.data = {}

        def inc(self, name):
            self.data[name] = self.data.get(name, 0) + 1

    stats = Stats()
    brain = make_brain(tmp_path, [GOOD, '{"should_reply": false}'], stats=stats)
    asyncio.run(brain.handle(chat()))
    asyncio.run(brain.handle(chat("。。。")))
    assert stats.data["agent_replied"] == 1
    assert stats.data["agent_no_reply"] == 1


def test_reset_session_clears_memory_and_recall_window(tmp_path):
    brain = make_brain(tmp_path, [GOOD])
    asyncio.run(brain.handle(chat()))
    assert brain.memory.spoken()
    brain.reset_session()
    assert brain.memory.spoken() == []
    assert brain.memory.events() == []


def test_brain_without_selector_falls_back_to_config_prompt(tmp_path):
    """存档缺失时也要能决策：底料退回 config.persona（与 rules 链路一致）。"""
    brain = AgentBrain(
        config={"llm": {"base_url": "u", "api_key": "k", "model": "m"},
                "persona": {"nickname": "仔仔鼠", "style": "热情", "max_reply_chars": 20},
                "game": {"name": "休闲小游戏", "intro": "挂机就行"}},
        llm=StubLLM([GOOD]),
        decision_log=DecisionLogger(log_dir=tmp_path, enabled=True),
    )
    result = asyncio.run(brain.handle(chat()))
    assert result["text"] == "兄弟点右下角小手柄就能下"
    assert "仔仔鼠" in brain._llm.calls[0]["system"]
