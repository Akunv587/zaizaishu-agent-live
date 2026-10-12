"""Router chat 聚合窗口的单元测试（PROMPTS 步骤 2.2）。

验收口径：假时钟与假随机数下——3 秒内三条相似弹幕只产出 1 个任务且合并数为 3；
相似与不相似混发产出多个任务；窗口到期触发产出；ignore_noise_p=1 时短文本全部忽略。
"""
import asyncio

import pytest

from hub.events import LiveEvent
from hub.router import Router, is_noise_text


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class SeqRandom:
    """random() 依次返回序列值，越界后返回最后一个。"""

    def __init__(self, values):
        self.values = list(values)
        self.i = 0

    def random(self):
        if self.i >= len(self.values):
            return self.values[-1]
        v = self.values[self.i]
        self.i += 1
        return v


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


def echo_builder(ctx):
    """同步假生成器：回显弹幕原文（占位文本已被「无生成器不产出」取代）。"""
    return f"回复：{ctx['content']}"


def make_router(tasks, clock, random=None, reply_builder=echo_builder, **cfg_over):
    cfg = {
        "events": {
            "chat": {"enabled": True, "merge_window_s": 3, "ignore_noise_p": 0.2},
        },
        "safety": {"fallback_reply": "哈哈这个咱们直播间聊哈"},
    }
    cfg["events"].update(cfg_over.pop("events_extra", {}))
    cfg.update(cfg_over)
    router = Router(tasks.append, config=cfg, clock=clock, random=random or SeqRandom([0.5]),
                    reply_builder=reply_builder)
    return router


def chat(user, content, ts):
    return LiveEvent(type="chat", user=user, content=content, ts=ts)


def drain(router):
    asyncio.run(router.drain(timeout=2))


def test_has_pending_reply_reflects_window():
    """合并窗口打开期间 has_pending_reply=True；到期处理完后回落 False。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock)
    assert router.has_pending_reply() is False        # 空闲

    router.handle_event(chat("甲", "这游戏怎么下载", 0))
    assert router.has_pending_reply() is True         # 合并窗口打开

    clock.advance(3.5)
    router.scan()                                     # 窗口到期 + 同步回显直接产出
    assert router.has_pending_reply() is False
    assert len(tasks) == 1


def test_dict_reply_carries_audio_path():
    """v3.1：reply_builder 返回 dict → SpeakTask 带 audio_path（预录音频应答）。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock,
                         reply_builder=lambda ctx: {"text": "听音频", "audio_path": "D:/a.wav"})
    router.handle_event(chat("甲", "这游戏怎么下载", 0))
    clock.advance(3.5)
    router.scan()
    drain(router)
    assert len(tasks) == 1
    assert tasks[0].text == "听音频"
    assert tasks[0].audio_path == "D:/a.wav"


def test_none_reply_skips_task():
    """v3.1：reply_builder 返回 None → 该组弹幕不产出任务（存档缺失等）。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, reply_builder=lambda ctx: None)
    router.handle_event(chat("甲", "在吗", 0))
    clock.advance(3.5)
    router.scan()
    drain(router)
    assert tasks == []


def test_empty_dict_reply_skips_task():
    """v3.1：dict 里 text/audio_path 全空 → 不产出空任务。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock,
                         reply_builder=lambda ctx: {"text": "", "audio_path": ""})
    router.handle_event(chat("甲", "在吗", 0))
    clock.advance(3.5)
    router.scan()
    drain(router)
    assert tasks == []


def test_three_similar_in_window_merge_to_one_task():
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock)
    router.handle_event(chat("甲", "主播这是什么游戏", 0))
    router.handle_event(chat("乙", "主播这是什么游戏呀", 1))
    router.handle_event(chat("丙", "主播这是什么游戏啊", 2))
    # 窗口未到期：尚无任务
    assert tasks == []
    # 推进 3.5 秒后扫描：窗口到期产出
    clock.advance(3.5)
    router.scan()
    drain(router)
    assert len(tasks) == 1
    assert tasks[0].priority == 1
    assert tasks[0].source == "chat"
    assert tasks[0].merged_count == 3
    # 代表弹幕为组内最早一条
    assert "主播这是什么游戏" in tasks[0].text
    assert tasks[0].event_ts == 0


def test_window_expiry_via_new_event():
    """到期后新弹幕到来触发旧窗口产出并开启新窗口（惰性 flush 路径）。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock)
    router.handle_event(chat("甲", "第一条", 0))
    clock.advance(10.0)
    router.handle_event(chat("乙", "完全不相关的新话题", 10))
    drain(router)
    assert len(tasks) == 1  # 旧窗口已产出
    clock.advance(10.0)
    router.scan()
    drain(router)
    assert len(tasks) == 2  # 新窗口到期产出


def test_dissimilar_chats_produce_multiple_tasks():
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock)
    router.handle_event(chat("甲", "主播这游戏叫什么名字", 0))
    router.handle_event(chat("乙", "在哪里可以下载到这款游戏呢", 1))
    router.handle_event(chat("丙", "666主播操作真秀", 2))
    clock.advance(3.5)
    router.scan()
    drain(router)
    assert len(tasks) == 3
    assert all(t.merged_count == 1 for t in tasks)


def test_similar_and_dissimilar_mixed_groups():
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock)
    router.handle_event(chat("甲", "这游戏好玩吗", 0))
    router.handle_event(chat("乙", "这游戏好玩吗？", 0.5))
    router.handle_event(chat("丙", "主播今天状态不错啊", 1))
    router.handle_event(chat("丁", "主播今天状态不错呀", 1.5))
    clock.advance(3.5)
    router.scan()
    drain(router)
    assert len(tasks) == 2
    assert sorted(t.merged_count for t in tasks) == [2, 2]


def test_ignore_noise_probability_one():
    """ignore_noise_p=1：短文本/纯符号弹幕全部被忽略。"""
    tasks, clock = [], FakeClock(0.0)
    cfg = {"events": {"chat": {"enabled": True, "merge_window_s": 3, "ignore_noise_p": 1.0}}}
    router = Router(tasks.append, config=cfg, clock=clock, random=SeqRandom([0.5]))
    router.handle_event(chat("甲", "6", 0))          # 长度 < 2
    router.handle_event(chat("乙", "😄🎉!!!", 0.5))   # 去符号后为空
    clock.advance(3.5)
    router.scan()
    drain(router)
    assert tasks == []


def test_ignore_noise_probability_zero_keeps_noise():
    """ignore_noise_p=0：噪声弹幕不忽略、照常聚合。"""
    tasks, clock = [], FakeClock(0.0)
    cfg = {"events": {"chat": {"enabled": True, "merge_window_s": 3, "ignore_noise_p": 0.0}}}
    router = Router(tasks.append, config=cfg, clock=clock, random=SeqRandom([0.9]),
                    reply_builder=echo_builder)
    router.handle_event(chat("甲", "😄", 0))
    clock.advance(3.5)
    router.scan()
    drain(router)
    assert len(tasks) == 1


def test_input_filter_blocks_chat():
    """步骤 2.2 预留的 input_filter 钩子：命中即丢弃。"""
    tasks, clock = [], FakeClock(0.0)
    router = Router(tasks.append,
                    config={"events": {"chat": {"enabled": True, "merge_window_s": 3}}},
                    clock=clock, random=SeqRandom([0.5]),
                    input_filter=lambda text: "敏感" in text)
    router.handle_event(chat("甲", "带敏感词的弹幕", 0))
    clock.advance(3.5)
    router.scan()
    drain(router)
    assert tasks == []


def test_similarity_default_impl():
    from hub.router import default_similarity
    assert default_similarity("主播这是什么游戏", "主播这是什么游戏") == 1.0
    assert default_similarity("主播这是什么游戏", "主播这是什么游戏呀") >= 0.6
    assert default_similarity("主播这是什么游戏", "完全不同的内容") < 0.6
    assert default_similarity("a", "a") == 0.0  # 不足 2-gram


def test_is_noise_text():
    assert is_noise_text("") is True
    assert is_noise_text("6") is True
    assert is_noise_text("   ") is True
    assert is_noise_text("!!!😄") is True
    assert is_noise_text("666") is False
    assert is_noise_text("正常弹幕") is False


def test_no_reply_builder_skips_chat_tasks():
    """LLM 未配置（reply_builder=None）：弹幕聚合但不产出任务（占位文本不再说出口）。"""
    tasks, clock = [], FakeClock(0.0)
    router = make_router(tasks, clock, reply_builder=None)
    router.handle_event(chat("甲", "主播这是什么游戏", 0))
    router.handle_event(chat("乙", "主播这是什么游戏呀", 1))
    clock.advance(3.5)
    router.scan()
    drain(router)
    assert tasks == []


def test_set_reply_builder_hot_swap():
    """控制台保存 LLM 配置后热替换生成器：None → 有 → None 三态切换即时生效。"""
    from hub.stats import StatsCollector

    tasks, clock = [], FakeClock(0.0)
    stats = StatsCollector()
    router = Router(tasks.append, config={"events": {"chat": {"enabled": True,
                                                            "merge_window_s": 3}}},
                    clock=clock, random=SeqRandom([0.5]), reply_builder=None,
                    stats=stats)
    router.handle_event(chat("甲", "第一条", 0))
    clock.advance(3.5)
    router.scan()
    assert tasks == []                       # 未配置：跳过
    assert stats.summary().get("chat_no_llm") == 1

    router.set_reply_builder(echo_builder)  # 热配置生效
    router.handle_event(chat("乙", "第二条", 10))
    clock.advance(3.5)
    router.scan()
    assert len(tasks) == 1
    assert "第二条" in tasks[0].text

    router.set_reply_builder(None)           # 热停用
    router.handle_event(chat("丙", "第三条", 20))
    clock.advance(3.5)
    router.scan()
    assert len(tasks) == 1
    assert stats.summary().get("chat_no_llm") == 2


def test_async_reply_builder_scheduled():
    """异步生成器（线上 Generator 形态）经后台任务补齐，不挡同步调用方。"""
    tasks, clock = [], FakeClock(0.0)

    async def async_builder(ctx):
        return f"异步回复：{ctx['content']}"

    router = make_router(tasks, clock, reply_builder=async_builder)

    async def scenario():
        router.handle_event(chat("甲", "异步弹幕", 0))
        clock.advance(3.5)
        router.scan()               # 循环内调度异步补齐（_schedule 需要运行中的事件循环）
        assert tasks == []          # 尚未补齐（异步在途）
        await router.drain(timeout=2)
        assert len(tasks) == 1
        assert tasks[0].text == "异步回复：异步弹幕"

    asyncio.run(scenario())
