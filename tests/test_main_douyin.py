"""main.py 内置采集器装配（步骤2 接线）的单元测试。

FakeCollector 替换 hub.douyin.DouyinCollector：验证 room_num 非空时
装配采集器协程、DyMessage dict 经 normalize 流入统计与路由、控制台
快照的 source 字段接的是采集器 status()、URL 形态房间号可提取。
"""
import asyncio
import json
import signal
import socket

import pytest
from websockets.asyncio.client import connect

import main as main_mod
from hub.stats import StatsCollector


@pytest.fixture(autouse=True)
def restore_sigint():
    yield
    signal.signal(signal.SIGINT, signal.default_int_handler)


@pytest.fixture
def logger_setup(tmp_path):
    from hub import logger as logger_mod

    logger_mod.setup(tmp_path, "info")
    return tmp_path


class _FakeCollector:
    """采集器替身：run() 推一条 DyMessage 后等 stop，status() 可控。"""

    instances = []

    def __init__(self, room_num, on_event, max_reconnect=3, cookie=""):
        self.room_num = room_num
        self.on_event = on_event
        self.cookie = cookie
        self.state = "live"
        self.messages = 1
        self.last_error = ""
        self.run_called = False
        self.run_ended = False
        self.status_calls = 0
        _FakeCollector.instances.append(self)

    async def run(self, stop):
        self.run_called = True
        self.on_event({"method": "WebcastChatMessage",
                       "user": {"name": "测试观众"}, "content": "主播玩什么游戏"})
        try:
            await stop.wait()
        finally:
            self.run_ended = True

    def status(self):
        self.status_calls += 1
        return {"mode": "builtin", "room_num": self.room_num, "room_id": "74",
                "title": "测试直播间", "anchor": "仔仔鼠", "state": self.state,
                "reconnects": 0, "messages": self.messages,
                "last_error": self.last_error}


@pytest.fixture
def fake_collector_cls():
    _FakeCollector.instances = []
    return _FakeCollector


def _offline(c) -> bool:
    """采集器是否已下线（判定旧连接被换房取消）。

    两个口径合起来才算数，缺一不可：
    - `run_ended`：协程体跑过并在 `finally` 里落了标记；
    - `not run_called`：**任务在 `run()` 拿到第一次调度之前就被取消**——协程体永不执行，
      `run_ended` 自然是假。这类取消在产品语义上同样是"旧采集器已下线"（任务已 done），
      但按 `run_ended` 单独判定会永远等不到，且在机器负载高时高概率复现
      （3333 的处理抢在 2222 的 `run()` 启动之前）。
    """
    return bool(c.run_ended) or not c.run_called


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_until(cond, timeout=5.0):
    import time
    t0 = time.monotonic()
    while not cond():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.02)


def test_concurrent_set_room_serialized_no_dual_collectors(
        logger_setup, fake_collector_cls, monkeypatch):
    """并发 set_room（双击/cookie 重连撞上手动换房）：connect_lock 串行化——
    前一个连接被取消后才建下一个，全程只有一个活采集器（防双连接互踢/限流）。

    无锁回归形态：两个协程都在 await 旧任务窗口期读到空 danmaku_ctl，各起一个
    采集器且互不知情 → 先建的那个永远没人取消。
    """
    monkeypatch.setattr("hub.douyin.DouyinCollector", fake_collector_cls)
    port = _free_port()
    cfg = {"app": {"log_level": "info"}, "danmaku": {"room_num": "1111"},
           "webui": {"enabled": True, "listen": f"127.0.0.1:{port}"}}
    stats = StatsCollector()
    settled = []

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(main_mod.run_main_loop(cfg, stats, stop))
        try:
            await wait_until(lambda: len(fake_collector_cls.instances) == 1
                             and fake_collector_cls.instances[0].run_called)
            async with connect(f"ws://127.0.0.1:{port}/ws", open_timeout=3) as ws:
                await asyncio.wait_for(ws.recv(), timeout=3)  # 首帧快照
                await ws.send(json.dumps({"action": "set_room", "room": "2222"}))
                await ws.send(json.dumps({"action": "set_room", "room": "3333"}))
                await wait_until(lambda: len(fake_collector_cls.instances) == 3
                                 and _offline(fake_collector_cls.instances[0])
                                 and _offline(fake_collector_cls.instances[1])
                                 # 存活那个必须真的跑起来，否则下面定格会把它记成"没上线"
                                 and fake_collector_cls.instances[2].run_called)
            settled.extend((c.room_num, _offline(c))   # 定格在 stop 前
                           for c in fake_collector_cls.instances)
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())

    assert settled == [("1111", True),      # 自动连接的初始房间被换房取消
                       ("2222", True),      # 锁内被后一个连接取消（无锁则存活 → 双采集器）
                       ("3333", False)]     # 唯一存活


def test_builtin_collector_assembled_and_events_flow(logger_setup, fake_collector_cls, monkeypatch):
    """room_num 非空：不装配 8765 外挂，采集器协程启动，DyMessage 流入统计。"""
    monkeypatch.setattr("hub.douyin.DouyinCollector", fake_collector_cls)
    cfg = {"app": {"log_level": "info"}, "danmaku": {"room_num": "8888"},
           "webui": {"enabled": True, "listen": "127.0.0.1:0"}}
    stats = StatsCollector()

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(main_mod.run_main_loop(cfg, stats, stop))
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())

    assert len(fake_collector_cls.instances) == 1
    fake = fake_collector_cls.instances[0]
    assert fake.room_num == "8888"
    assert fake.run_called
    # DyMessage dict 经适配层 normalize → LiveEvent → 计数进统计（与 ws_server 同链路）
    assert stats.summary()["danmaku_in"] == 1


def test_builtin_snapshot_source_is_collector_status(logger_setup, fake_collector_cls, monkeypatch):
    """控制台快照的 source 字段来自采集器 status()（state=live 时前端亮绿灯）。"""
    monkeypatch.setattr("hub.douyin.DouyinCollector", fake_collector_cls)
    port = _free_port()
    cfg = {"app": {"log_level": "info"},
           "danmaku": {"room_num": "https://live.douyin.com/8888?entry=1"},
           "webui": {"enabled": True, "listen": f"127.0.0.1:{port}"}}
    stats = StatsCollector()

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(main_mod.run_main_loop(cfg, stats, stop))
        await asyncio.sleep(0.2)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws", open_timeout=3) as ws:
                snap = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                assert snap["type"] == "snapshot"
                source = snap["source"]
                # URL 形态房间号被提取为纯数字
                assert source["mode"] == "builtin"
                assert source["room_num"] == "8888"
                assert source["state"] == "live"
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())


def test_builtin_collector_receives_cookie_from_config(logger_setup, fake_collector_cls, monkeypatch):
    """config.yaml danmaku.cookie 传递给采集器（粘贴登录态直连）。"""
    monkeypatch.setattr("hub.douyin.DouyinCollector", fake_collector_cls)
    cfg = {"app": {"log_level": "info"},
           "danmaku": {"room_num": "8888", "cookie": "ttwid=x; sessionid=y"},
           "webui": {"enabled": False}}
    stats = StatsCollector()

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(main_mod.run_main_loop(cfg, stats, stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())
    fake = fake_collector_cls.instances[0]
    assert fake.cookie == "ttwid=x; sessionid=y"


def test_set_room_hot_switches_collector(logger_setup, fake_collector_cls, monkeypatch):
    """控制台 set_room：停旧采集器、按新房间号重建，source_state 切到新实例。"""
    monkeypatch.setattr("hub.douyin.DouyinCollector", fake_collector_cls)
    port = _free_port()
    cfg = {"app": {"log_level": "info"}, "danmaku": {"room_num": "8888"},
           "webui": {"enabled": True, "listen": f"127.0.0.1:{port}"}}
    stats = StatsCollector()

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(main_mod.run_main_loop(cfg, stats, stop))
        await asyncio.sleep(0.2)
        try:
            async with connect(f"ws://127.0.0.1:{port}/ws", open_timeout=3) as ws:
                await asyncio.wait_for(ws.recv(), timeout=3)  # snapshot
                await asyncio.wait_for(ws.recv(), timeout=3)  # feed
                await ws.send(json.dumps({"action": "set_room",
                                           "room": "https://live.douyin.com/9999"}))
                await asyncio.sleep(0.4)
                snap = None
                # 等下一秒快照确认 source 已切到新房间
                for _ in range(6):
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                    if msg["type"] == "snapshot":
                        snap = msg
                        if snap["source"].get("room_num") == "9999":
                            break
                assert snap is not None and snap["source"]["room_num"] == "9999"
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    assert len(fake_collector_cls.instances) == 2
    assert fake_collector_cls.instances[0].room_num == "8888"
    assert fake_collector_cls.instances[1].room_num == "9999"


@pytest.mark.parametrize("raw,expected", [
    ("8888", "8888"),
    ("  8888 ", "8888"),
    ("https://live.douyin.com/8888", "8888"),
    ("https://live.douyin.com/8888?entry_id=xxx", "8888"),
    ("https://live.douyin.com/8888/", "8888"),
    ("http://live.douyin.com/1234567890123", "1234567890123"),
    ("abc", "abc"),   # 非数字原样返回，交采集器请求直播页时给出可读报错
])
def test_extract_room_num(raw, expected):
    assert main_mod._extract_room_num(raw) == expected
