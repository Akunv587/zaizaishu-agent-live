"""hub.replay 重放事件源的单元测试（PROMPTS 步骤 2.1）。

验收口径：时间戳间隔 2 秒的 10 行 JSONL，speed=100 快速回放，
on_event 收到 10 条且顺序与文件一致；统计数值正确；异常输入不中断。

项目约定：不引入 pytest-asyncio，异步场景用同步测试包 asyncio.run()。
"""
import asyncio
import json

import pytest

from hub.events import LiveEvent
from hub.replay import ReplaySource

BASE_TS = 1_700_000_000_000.0  # 毫秒


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def chat_msg(name: str, text: str):
    return {"method": "WebcastChatMessage", "user": {"name": name}, "content": text}


@pytest.fixture
def ten_lines(tmp_path):
    """10 行 chat 消息，时间戳间隔 2 秒。"""
    rows = []
    for i in range(10):
        rows.append({
            "timestamp": BASE_TS + i * 2000,
            "method": "WebcastChatMessage",
            "user": {"name": f"观众{i}"},
            "content": f"第{i}条弹幕",
        })
    path = tmp_path / "ten.jsonl"
    write_jsonl(path, rows)
    return path


def test_replay_ten_lines_order_and_count(ten_lines):
    got = []

    async def scenario():
        async def on_event(ev: LiveEvent):
            got.append(ev)

        src = ReplaySource(str(ten_lines), on_event, speed=100)
        await src.run()

    asyncio.run(scenario())

    assert len(got) == 10
    assert [ev.content for ev in got] == [f"第{i}条弹幕" for i in range(10)]
    assert [ev.user for ev in got] == [f"观众{i}" for i in range(10)]
    assert all(ev.type == "chat" for ev in got)


def test_replay_stats(ten_lines):
    stats = {}

    async def scenario():
        async def on_event(ev):
            pass

        src = ReplaySource(str(ten_lines), on_event, speed=100)
        await src.run()
        stats.update(src.stats())

    asyncio.run(scenario())

    assert stats["total_events"] == 10
    assert stats["by_type"] == {"chat": 10}
    assert stats["duration_s"] == 18.0  # 9 个间隔 × 2 秒
    assert stats["speed"] == 100


def test_replay_sync_callback_supported(ten_lines):
    """on_event 允许同步函数（ws_server 同一约定）。"""
    got = []

    async def scenario():
        src = ReplaySource(str(ten_lines), got.append, speed=100)
        await src.run()

    asyncio.run(scenario())
    assert len(got) == 10


def test_replay_skips_live_info_and_bad_lines(tmp_path):
    """首行直播间信息对象、损坏 JSON 行、非白名单行均跳过且不中断。"""
    rows = [
        {"roomId": 123, "nickname": "仔仔鼠", "title": "测试间"},  # 无 method：直播间信息
        dict(chat_msg("甲", "你好"), timestamp=BASE_TS),
        "<<<损坏行>>>",
        {"timestamp": BASE_TS + 500, "method": "WebcastLikeMessage", "user": {"name": "乙"}},  # 白名单外
        {"timestamp": BASE_TS + 1000, "method": "WebcastChatMessage",
         "user": {"name": "丙"}, "content": "第二句"},
    ]
    path = tmp_path / "mixed.jsonl"
    # 损坏行必须是非法 JSON 文本，手工写文件
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            if isinstance(row, str):
                f.write(row + "\n")
            else:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    got = []

    async def scenario():
        src = ReplaySource(str(path), got.append, speed=50)
        await src.run()

    asyncio.run(scenario())
    assert [ev.content for ev in got] == ["你好", "第二句"]


def test_replay_missing_field_line_skipped(tmp_path):
    """必填字段缺失的行经 normalize 返回 None，不计入总数。"""
    rows = [
        {"timestamp": BASE_TS, "method": "WebcastChatMessage", "content": "没有昵称"},
        {"timestamp": BASE_TS + 1000, "method": "WebcastChatMessage",
         "user": {"name": "丁"}, "content": "正常"},
    ]
    path = tmp_path / "bad-field.jsonl"
    write_jsonl(path, rows)

    got = []
    total = {}

    async def scenario():
        src = ReplaySource(str(path), got.append, speed=100)
        await src.run()
        total.update(src.stats())

    asyncio.run(scenario())
    assert len(got) == 1
    assert total["total_events"] == 1


def test_replay_empty_file(tmp_path):
    """空文件：记 warning 后直接返回（文件不存在的校验由 main.py 启动前拦截）。"""
    done = {}

    async def scenario():
        async def on_event(ev):
            pass

        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        src = ReplaySource(str(empty), on_event)
        await src.run()
        done.update(src.stats())

    asyncio.run(scenario())
    assert done["total_events"] == 0


def test_replay_missing_timestamp_inherits_previous(tmp_path):
    """缺 timestamp 的行沿用最近已知时间戳：不产生天文数字级等待。"""
    rows = [
        {"timestamp": BASE_TS, "method": "WebcastChatMessage",
         "user": {"name": "甲"}, "content": "带时间戳"},
        {"method": "WebcastChatMessage", "user": {"name": "乙"}, "content": "不带时间戳"},
        {"timestamp": BASE_TS + 1000, "method": "WebcastChatMessage",
         "user": {"name": "丙"}, "content": "又带"},
    ]
    path = tmp_path / "no-ts.jsonl"
    write_jsonl(path, rows)

    got = []

    async def scenario():
        src = ReplaySource(str(path), got.append, speed=50)
        await src.run()

    # 若缺时间戳按 0 处理，此处会 sleep 约 3.4e7 秒导致挂死——测试能在秒级完成即验证了兜底
    asyncio.run(asyncio.wait_for(scenario(), timeout=10))
    assert [ev.content for ev in got] == ["带时间戳", "不带时间戳", "又带"]


def test_replay_speed_clamp():
    """speed 下限保护，避免 0 或负数导致除零。"""
    src = ReplaySource("whatever.jsonl", lambda ev: None, speed=0)
    assert src._speed == 0.01
    src = ReplaySource("whatever.jsonl", lambda ev: None, speed=-5)
    assert src._speed == 0.01
