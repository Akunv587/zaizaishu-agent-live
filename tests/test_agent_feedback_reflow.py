"""反馈样本回流（few-shot 注入）的单元与装配测试（M6-2）。

验收口径：
- 样本库**每次决策现读**：点完采纳，下一条决策立刻能召回它（V6.4 的机制层）；
- 传了 provider 就完全走 provider（静态 `examples` 让位，不叠加）；
- provider 抛异常 → 退化成"这次没有样本"，不挡回复；
- 不传 provider 时静态 `examples` 照旧可用（向后兼容 M4 的用法）；
- **main 装配接线**：真实 `run_main_loop` 起完，`webui.feedback_store` 绑到活动存档目录，
  且 `AgentBrain` 的示例来源就是这个 store 的 `examples`。
"""
import asyncio
from pathlib import Path

import pytest

import main as main_mod
from hub import logger as logger_mod
from hub import router as router_mod
from hub import webui as webui_mod
from hub.agent.brain import AgentBrain
from hub.agent.feedback import FeedbackStore
from hub.queue import PrioritySpeakQueue
from hub.router import Router
from hub.stats import StatsCollector

CONFIG_YAML = """\
app:
  log_level: info
danmaku:
  ws_listen: 127.0.0.1:0

agent:
  enabled: true
"""

ENTRY = {
    "event_type": "chat",
    "user": "小明",
    "content": "主播玩什么游戏",
    "reply": "玩地铁跑酷，猜猜我今晚上几分",
    "tone": "happy",
    "reason": "命中游戏话题",
}


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


class StubLLM:
    """把模型换成纯函数：只回一句固定文本，不碰网络。"""

    def __init__(self, *a, **kw):
        self.model = "stub-model"
        self.seen = []

    async def ask(self, system: str, user: str) -> str:
        self.seen.append((system, user))
        return ('{"should_reply": true, "priority": 1, "interrupt": false, '
                '"tone": "热情", "emotion": "happy", "speed": null, '
                '"reason": "有效提问", "reply_text": "地铁跑酷，来一起玩", '
                '"memory_note": ""}')

    def last_system(self) -> str:
        return self.seen[-1][0]


def make_brain(tmp_path, *, examples=None, provider=None, stub=None):
    stub = stub or StubLLM()
    brain = AgentBrain(
        config={"persona": {"max_reply_chars": 40},
                "agent": {"retrieve": {"layer": "l2", "top_k": 5}}},
        settings={"retrieve": {"layer": "l2", "top_k": 5}},
        llm=stub, examples=examples, examples_provider=provider,
        log_dir=tmp_path)
    return brain, stub


def ctx(content="主播玩什么游戏"):
    return {"type": "chat", "user": "小明", "content": content}


def _example(eid: str, reply: str = "地铁跑酷，来一起玩") -> dict:
    """静态示例必须与 `L2Retriever.index()` 的 examples 契约同形状：

    `reply` + `event.content` —— 这正是"单测别喂理想形状"那条规矩的现场。
    """
    return {"id": eid, "reply": reply, "event": {"content": "主播玩什么游戏"}}


# ---------------------------------------------------------------- 现读


def test_adopted_sample_is_recalled_on_next_decision(tmp_path):
    """点完采纳，下一条决策立刻召回它——不做缓存、不做失效逻辑。"""
    store = FeedbackStore(root=tmp_path)
    brain, stub = make_brain(tmp_path, provider=store.examples)

    first = asyncio.run(brain.preview(ctx()))
    assert first["recall"] == []                       # 还没样本
    assert "地铁跑酷，猜猜我今晚上几分" not in stub.last_system()

    rec = store.adopt(ENTRY)

    second = asyncio.run(brain.preview(ctx()))
    assert second["recall"] == [f"example:{rec['id']}"]
    assert "地铁跑酷，猜猜我今晚上几分" in stub.last_system()   # 进了 prompt 的召回段


def test_v64_cumulative_adoptions_change_the_prompt(tmp_path):
    """V6.4 的可测部分：累计 10 条好评后，同一条事件送进模型的提示词**变了**
    （多条样本一起进 few-shot 召回段）。

    真实"回复语气可见变化"要真模型才能看出来，单测只能验到"输入被改变了"这一层——
    把这一层验死，"累积后没变化"就不可能发生在这个模块里。
    """
    store = FeedbackStore(root=tmp_path)
    brain, stub = make_brain(tmp_path, provider=store.examples)
    asyncio.run(brain.preview(ctx()))
    before = stub.last_system()

    for i in range(10):
        store.adopt({**ENTRY, "reply": f"第{i + 1}条好评参考，语气接地气一点"})

    got = asyncio.run(brain.preview(ctx()))
    after = stub.last_system()
    assert after != before
    # 检索是**排序取 top_k**，不是全量塞：所以断"进去多条"而不是"第 10 条一定在"
    # （10 条样本内容近似，谁进 top_k 由打分与并列顺序决定）
    hits = [i for i in range(1, 11) if f"第{i}条好评参考" in after]
    assert len(hits) >= 2, after
    assert not any(f"第{i}条好评参考" in before for i in range(1, 11))
    assert got["recall"] and all(r.startswith("example:") for r in got["recall"])


def test_rejected_sample_never_recalled(tmp_path):
    """弃用的样本不进 few-shot（V6.2 的机制层）。"""
    store = FeedbackStore(root=tmp_path)
    store.reject(ENTRY, ["太长"])
    brain, stub = make_brain(tmp_path, provider=store.examples)

    got = asyncio.run(brain.preview(ctx()))
    assert got["recall"] == []
    assert "猜猜我今晚上几分" not in stub.last_system()


def test_rewritten_sample_enters_with_new_text(tmp_path):
    """改写后入库的是**改后文本**，召回的是它。"""
    store = FeedbackStore(root=tmp_path)
    rec = store.rewrite(ENTRY, "地铁跑酷，来猜猜我几分")
    brain, stub = make_brain(tmp_path, provider=store.examples)

    got = asyncio.run(brain.preview(ctx()))
    assert got["recall"] == [f"example:{rec['id']}"]
    assert "来猜猜我几分" in stub.last_system()
    assert "猜猜我今晚上几分" not in stub.last_system()


def test_provider_takes_precedence_over_static_examples(tmp_path):
    """传了 provider 就不再叠加静态 examples——避免两条来源同时喂、条数翻倍。"""
    store = FeedbackStore(root=tmp_path)          # 空样本库
    static = _example("static1", "静态样本不该出现")
    brain, stub = make_brain(tmp_path, examples=[static],
                             provider=store.examples)
    got = asyncio.run(brain.preview(ctx()))
    assert got["recall"] == []
    assert "静态样本不该出现" not in stub.last_system()


def test_static_examples_still_work_without_provider(tmp_path):
    """不传 provider 时静态 examples 照旧（M4 的用法不能被这次改动破坏）。"""
    brain, stub = make_brain(tmp_path, examples=[_example("s9")])
    got = asyncio.run(brain.preview(ctx()))
    assert got["recall"] == ["example:s9"]


def test_provider_failure_degrades_to_no_examples(tmp_path):
    """样本读失败只降级为"这次没样本"，不能挡住回复。"""
    def boom():
        raise OSError("jsonl 被占用")

    brain, _ = make_brain(tmp_path, provider=boom)
    got = asyncio.run(brain.preview(ctx()))
    assert got["recall"] == []
    assert got["should_reply"] is True                # 照样给出决策


def test_provider_returning_none_is_tolerated(tmp_path):
    brain, _ = make_brain(tmp_path, provider=lambda: None)
    got = asyncio.run(brain.preview(ctx()))
    assert got["recall"] == []


def test_example_entries_have_no_prerecorded_audio(tmp_path):
    """示例条目没有 audios 键 → 命中示例只会现场合成，不会误播别条音频。

    `preview()` 返回的是**决策日志形状**的记录（无 `audio_path` 键，只有 `prerecorded`
    布尔），所以这里断的是真产物上真实存在的字段。
    """
    store = FeedbackStore(root=tmp_path)
    store.adopt(ENTRY)
    brain, _ = make_brain(tmp_path, provider=store.examples)
    got = asyncio.run(brain.preview(ctx()))
    assert got["recall"] == [f"example:{store.examples()[0]['id']}"]
    assert got["prerecorded"] is False


def test_feedback_audio_resolution_returns_empty(tmp_path):
    """直接对 FeedbackStore 的记录问音频：应得空串（真正的接缝断言）。"""
    from hub.selector import ScriptRuntime

    store = FeedbackStore(root=tmp_path)
    store.adopt(ENTRY)
    rt = ScriptRuntime.__new__(ScriptRuntime)         # 不跑构造，仅验纯函数分支
    rt._profile_id = "p_test"
    rt._profile = None
    assert rt.resolve_audio(store.examples()[0], "happy") == ""


# ---------------------------------------------------------------- main 装配


class RecordingRouter(Router):
    last = None

    def __init__(self, *a, **kw):
        RecordingRouter.last = self
        super().__init__(*a, **kw)


class RecordingQueue(PrioritySpeakQueue):
    last = None

    def __init__(self, *a, **kw):
        RecordingQueue.last = self
        super().__init__(*a, **kw)


class RecordingWebUi(webui_mod.WebUi):
    captured: dict = {}

    def __setattr__(self, name, value):
        if name in ("feedback_store", "script_runtime", "script_store"):
            RecordingWebUi.captured[name] = value
        super().__setattr__(name, value)


@pytest.fixture
def record(monkeypatch):
    RecordingRouter.last = None
    RecordingQueue.last = None
    RecordingWebUi.captured = {}
    monkeypatch.setattr(router_mod, "Router", RecordingRouter)
    monkeypatch.setattr(webui_mod, "WebUi", RecordingWebUi)
    monkeypatch.setattr("hub.queue.PrioritySpeakQueue", RecordingQueue)
    return RecordingWebUi.captured


def test_main_wires_feedback_store_into_brain(logger_setup, tmp_path, record):
    """真实装配：store 落在活动存档目录下，brain 的样本来源就是它的 `examples`。"""
    cfg = {"app": {"log_level": "info"}, "danmaku": {"ws_listen": "127.0.0.1:0"},
           "_config_path": str(tmp_path / "config.yaml"),
           "llm": {"base_url": "https://example.com/v1", "api_key": "sk-test",
                   "model": "m", "timeout_seconds": 8},
           "agent": {"enabled": True}}
    (tmp_path / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    stats = StatsCollector()
    captured = {}

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(main_mod.run_main_loop(cfg, stats, stop))
        await asyncio.sleep(0.05)
        captured["router"] = RecordingRouter.last
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())

    store = record["feedback_store"]
    rt = record["script_runtime"]
    assert isinstance(store, FeedbackStore)
    assert store.pid == rt.profile_id                  # 跟随活动存档
    assert store.examples_path().parts[-3:] == (rt.profile_id, "agent", "examples.jsonl")
    assert Path(rt.profile_id) .name == rt.profile_id  # 存档 id 形态（防御性）

    handler = captured["router"]._agent_handler
    assert handler is not None and captured["router"].agent_attached
    brain = handler.__self__
    assert isinstance(brain, AgentBrain)
    # 绑定的就是同一份 store 的现读方法（不是复制、不是别的来源）
    assert brain._examples_provider == store.examples
