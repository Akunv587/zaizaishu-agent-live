"""main.run_main_loop 的单元测试（PROMPTS 步骤 0.2）。

真实控制台 Ctrl+C 走 signal handler（Windows 主线程有效）+ KeyboardInterrupt
兜底双重路径，属 Python 标准行为；此处覆盖注入 stop 的退出语义与 handler 注册。
"""
import asyncio
import signal

import pytest

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


def test_reconfigure_stdio_switches_gbk_to_utf8():
    """gbk 控制台（Windows 默认）被切成 utf-8：emoji 弹幕打印不再抛异常丢弹幕。"""
    import io
    import sys

    buf = io.BytesIO()
    gbk_stream = io.TextIOWrapper(buf, encoding="gbk", errors="strict")
    old = sys.stdout
    sys.stdout = gbk_stream
    try:
        main_mod._reconfigure_stdio()
        assert sys.stdout.encoding.lower().replace("-", "").replace("_", "") == "utf8"
        sys.stdout.write("弹幕😘")
        sys.stdout.flush()  # 不应抛 UnicodeEncodeError
    finally:
        sys.stdout = old


def test_run_main_loop_exits_cleanly_on_stop(logger_setup, tmp_path):
    # ws_listen 用 0 端口（系统自动分配）：测试不与线上运行的中枢实例抢 8765
    cfg = {"app": {"log_level": "info"}, "danmaku": {"ws_listen": "127.0.0.1:0"}}
    stats = StatsCollector()

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(main_mod.run_main_loop(cfg, stats, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())

    content = (tmp_path / f"live-{__import__('datetime').datetime.now().strftime('%Y%m%d')}.log").read_text(encoding="utf-8")
    assert "main_loop_started" in content
    assert "main_loop_stopped" in content


def test_run_main_loop_registers_and_triggers_handler(logger_setup):
    cfg = {"danmaku": {"ws_listen": "127.0.0.1:0"}}
    stats = StatsCollector()
    original = signal.getsignal(signal.SIGINT)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(main_mod.run_main_loop(cfg, stats, stop))
        await asyncio.sleep(0.05)
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler) and handler is not original
        handler(None, None)  # 模拟 SIGINT 到达：handler 经 call_soon_threadsafe 置位 stop
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
