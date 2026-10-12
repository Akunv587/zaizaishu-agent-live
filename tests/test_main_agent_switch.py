"""main 智能体总开关装配的单元测试（M3）。

验收口径：
- V3.1 出厂 OFF：Router=rules、Queue no_expire=False；
- V3.3 配置开了但未配 LLM Key → 降级为规则管道，开关拒绝开启；
- V3.2 装配的 switcher 写回 config.yaml 并热切 Router/Queue（无需重启）；
- 状态 provider 反映运行时真实模式（含 configured_enabled / downgraded）。
"""
import asyncio

import pytest
import yaml

import main as main_mod
from hub import router as router_mod
from hub import webui as webui_mod
from hub.queue import PrioritySpeakQueue
from hub.router import Router
from hub.stats import StatsCollector

CONFIG_YAML = """\
# 顶注
app:
  log_level: info
danmaku:
  ws_listen: 127.0.0.1:0

agent:
  enabled: false        # 总开关
  retrieve:
    layer: l2
"""


@pytest.fixture(autouse=True)
def restore_sigint():
    import signal
    yield
    signal.signal(signal.SIGINT, signal.default_int_handler)


@pytest.fixture
def logger_setup(tmp_path):
    from hub import logger as logger_mod
    logger_mod.setup(tmp_path, "info")
    return tmp_path


class RecordingRouter(Router):
    last: Router = None

    def __init__(self, *a, **kw):
        RecordingRouter.last = self
        super().__init__(*a, **kw)


class RecordingQueue(PrioritySpeakQueue):
    last: PrioritySpeakQueue = None

    def __init__(self, *a, **kw):
        RecordingQueue.last = self
        super().__init__(*a, **kw)


class RecordingWebUi(webui_mod.WebUi):
    captured: dict = {}

    def __setattr__(self, name, value):
        if name in ("agent_status_provider", "agent_switcher"):
            RecordingWebUi.captured[name] = value
        super().__setattr__(name, value)


@pytest.fixture
def record(monkeypatch):
    RecordingRouter.last = None
    RecordingQueue.last = None
    RecordingWebUi.captured = {}
    monkeypatch.setattr(router_mod, "Router", RecordingRouter)
    monkeypatch.setattr(webui_mod, "WebUi", RecordingWebUi)
    # main 在函数内 `from hub.queue import PrioritySpeakQueue`，补丁打到该模块属性上
    monkeypatch.setattr("hub.queue.PrioritySpeakQueue", RecordingQueue)
    return RecordingWebUi.captured


def run_startup(cfg, tmp_path, agent_enabled, llm=True, extra=None):
    cfg = dict(cfg)
    cfg["app"] = {"log_level": "info"}
    cfg["danmaku"] = {"ws_listen": "127.0.0.1:0"}
    cfg["_config_path"] = str(tmp_path / "config.yaml")
    (tmp_path / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    if llm:
        cfg["llm"] = {"base_url": "https://example.com/v1", "api_key": "sk-test",
                      "model": "m", "timeout_seconds": 8}
    cfg["agent"] = {"enabled": agent_enabled}
    if extra:
        cfg["agent"].update(extra)

    stats = StatsCollector()
    captured = {}

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(main_mod.run_main_loop(cfg, stats, stop))
        await asyncio.sleep(0.05)
        captured["router"] = RecordingRouter.last
        captured["queue"] = RecordingQueue.last
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    captured["cfg"] = cfg
    return captured


# ---------- 启动口径 ----------

def test_startup_defaults_to_rules_mode(logger_setup, tmp_path, record):
    """V3.1：未开开关时 Router=rules、队列仍按超期丢弃，行为与旧版一致。"""
    got = run_startup({}, tmp_path, agent_enabled=False)
    assert got["router"].mode == "rules"
    assert got["queue"].no_expire is False
    assert "agent_switcher" in record and "agent_status_provider" in record


def test_startup_agent_on_with_llm(logger_setup, tmp_path, record):
    got = run_startup({}, tmp_path, agent_enabled=True, llm=True)
    assert got["router"].mode == "agent"
    assert got["queue"].no_expire is True


def test_startup_agent_downgraded_without_llm(logger_setup, tmp_path, record):
    """V3.3：配置开了但未配 LLM Key → 以规则管道启动（不静默改配置文件）。"""
    got = run_startup({}, tmp_path, agent_enabled=True, llm=False)
    assert got["router"].mode == "rules"
    assert got["queue"].no_expire is False
    # 配置文件不被擅自改写：用户配好 Key 后重启即可生效
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(text)["agent"]["enabled"] is False


# ---------- 状态 provider ----------

def test_status_provider_reports_runtime_mode(logger_setup, tmp_path, record):
    got = run_startup({}, tmp_path, agent_enabled=False)
    st = record["agent_status_provider"]()
    assert st["enabled"] is False and st["mode"] == "rules"
    assert st["no_expire"] is False
    assert st["configured_enabled"] is False and st["downgraded"] is False
    assert st["available"] is True                       # LLM 已配
    assert set(st["settings"]) >= {"retrieve", "memory", "emotion"}


def test_status_provider_flags_downgrade(logger_setup, tmp_path, record):
    run_startup({}, tmp_path, agent_enabled=True, llm=False)
    st = record["agent_status_provider"]()
    assert st["enabled"] is False and st["mode"] == "rules"   # 运行时真实状态
    assert st["configured_enabled"] is True
    assert st["available"] is False
    assert st["downgraded"] is True and "LLM" in st["note"]


# ---------- 热切换 ----------

def test_switcher_writes_config_and_switches_runtime(logger_setup, tmp_path, record):
    """V3.2：switcher 一次到位——写 config.yaml + 切 Router/Queue，无需重启。"""
    got = run_startup({}, tmp_path, agent_enabled=False)
    switcher = record["agent_switcher"]
    path = tmp_path / "config.yaml"

    ok, message = asyncio.run(switcher(True))
    assert ok is True and message
    assert got["router"].mode == "agent"
    assert got["queue"].no_expire is True
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["agent"]["enabled"] is True
    assert data["app"]["log_level"] == "info"            # 其他配置与注释未被破坏
    assert "# 顶注" in path.read_text(encoding="utf-8")
    assert "# 总开关" in path.read_text(encoding="utf-8")
    assert got["cfg"]["agent"]["enabled"] is True        # 内存 cfg 同步

    ok, message = asyncio.run(switcher(False))
    assert ok is True
    assert got["router"].mode == "rules"
    assert got["queue"].no_expire is False
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["agent"]["enabled"] is False


def test_switcher_refuses_without_llm_key(logger_setup, tmp_path, record):
    """V3.3：未配 LLM Key 时拒绝开启；配置文件与运行时都不动。"""
    got = run_startup({}, tmp_path, agent_enabled=False, llm=False)
    before = (tmp_path / "config.yaml").read_text(encoding="utf-8")

    ok, message = asyncio.run(record["agent_switcher"](True))
    assert ok is False and "LLM" in message
    assert got["router"].mode == "rules"
    assert got["queue"].no_expire is False
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == before


def test_switcher_write_failure_leaves_runtime_untouched(logger_setup, tmp_path, record):
    """落盘失败就整个不动：避免「这次生效、重启又回去」的假象。"""
    got = run_startup({}, tmp_path, agent_enabled=False)
    got["cfg"]["_config_path"] = str(tmp_path / "nope" / "config.yaml")   # 目录不存在

    ok, message = asyncio.run(record["agent_switcher"](True))
    assert ok is False and "config.yaml" in message
    assert got["router"].mode == "rules"
    assert got["queue"].no_expire is False
