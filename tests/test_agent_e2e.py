"""M4-5 验收：接管链路的端到端联调（V4.1 ~ V4.7）。

分两层，都不复制业务逻辑：

**A. main 装配接线**（真实 `run_main_loop`）
M4-4 把 `AgentBrain` 接进 `main.py`，但接点在 `run_main_loop` 的闭包里
（`_make_brain` / `_attach_agent`），常规单测覆盖不到。这里走真实启动流程核对：
钩子随总开关在启动与热切换时正确挂上 / 摘下，状态 provider 的 `enabled`
反映的是「mode + 钩子」双条件（`Router._agent_active`），而不是光看 mode。

**B. 整场重放**（真实 `Router` + 真实 `PrioritySpeakQueue` + `AgentBrain` + 桩 LLM）
`replay/rehearsal-001.jsonl` 含五类事件与三档礼物。跑完后逐项核对：

| # | 验收点 |
|---|---|
| V4.1 | 一场 14 条事件 → 14 条决策，字段齐全（九字段契约） |
| V4.2 | 截断 / 混说明文字 / 纯散文 / JSON 残骸 四种非法输出不崩、不把残骸念出去 |
| V4.3 | `should_reply=false` 的事件确实不产出任务，但仍进记忆 |
| V4.4 | 情绪三种处置（档位内 / 8 枚举内未勾 / 非法）在主流程落地正确 |
| V4.5 | 决策日志每行一个 JSON，可 `json.loads` |
| V4.6 | 单场记忆生效：同用户连刷礼物可识别、不回也记、说过的话进防复读缓冲 |
| V4.7 | 原样采纳召回素材 → 复用其预录音频；新写的句子不误用 |

V4.8（全量测试全绿）由 `pytest tests -q` 本身承载。
"""
import asyncio
import json
from pathlib import Path

import pytest

import main as main_mod
from hub import logger as logger_mod
from hub import webui as webui_mod
from hub.agent.brain import AgentBrain
from hub.agent.log import DecisionLogger
from hub.events import LiveEvent
from hub.queue import PrioritySpeakQueue
from hub.replay import ReplaySource
from hub.router import MODE_AGENT, Router
from hub.stats import StatsCollector

REPLAY_FILE = Path(__file__).resolve().parent.parent / "replay" / "rehearsal-001.jsonl"


@pytest.fixture(autouse=True)
def logger_setup(tmp_path):
    logger_mod.setup(tmp_path, "warning")
    return tmp_path


# ================================================================ 公共装配

CFG = {
    "events": {
        "chat": {"enabled": True, "merge_window_s": 3, "ignore_noise_p": 0.2},
        "gift": {"enabled": True, "combo_timeout_s": 5},
        "follow": {"enabled": True, "cooldown_s": 30, "probability": 0.6},
        "fansclub": {"enabled": True, "cooldown_s": 30, "probability": 0.8},
        "enter": {"enabled": True},
    },
    "safety": {"fallback_reply": "哈哈这个咱们直播间聊哈"},
    "llm": {"base_url": "https://example.com/v1", "api_key": "sk-test",
            "model": "stub-model", "timeout_seconds": 8},
    "persona": {"max_reply_chars": 40},
    "agent": {
        "enabled": True,
        "retrieve": {"layer": "l2", "top_k": 5},
        "memory": {"max_events": 50, "max_spoken": 20, "max_notes": 10},
        "emotion": {"enabled": True, "onsite_fallback": True},
        "decision_log": True,
    },
}

# 存档素材：s1 有预录音频、s2 没有（V4.7 的一正一反两个样本）
PROFILE = {
    "samples_ai": [
        {"id": "s1", "enabled": True, "type_name": "问下载",
         "examples": ["怎么下载", "在哪下载啊"],
         "reply_text": "右下角小手柄点一下就能下载"},
        {"id": "s2", "enabled": True, "type_name": "问机型",
         "examples": ["这个手机能玩吗", "什么手机能玩"],
         "reply_text": "手机能玩，进游戏挂着就行"},
    ],
    "topics_ai": [{"id": "t1", "enabled": True, "text": "这游戏主打休闲挂机"}],
}


class StubLLM:
    """把「模型」换成一个纯函数：`responder(system, user) -> 原始输出`。"""

    def __init__(self, responder, model="stub-model"):
        self.model = model
        self._responder = responder
        self.calls = []

    async def ask(self, system, user):
        self.calls.append({"system": system, "user": user})
        return self._responder(system, user)


class StubSelector:
    """按 `ScriptRuntime` 的用到的四个入口做等价替身（存档 + 音频解析）。"""

    def __init__(self, profile=None, emotions=("default", "happy", "calm"),
                 audio=None, max_reply_chars=40):
        self.profile = PROFILE if profile is None else profile
        self._emotions = list(emotions)
        self._audio = dict(audio if audio is not None else {"s1": "C:/audio/s1.mp3"})
        self._max_reply_chars = max_reply_chars
        self.audio_queries = []

    def build_system_prompt(self, base_cfg=None):
        return "你是抖音游戏主播「仔仔鼠」，风格热情接地气。"

    def current_emotions(self):
        return list(self._emotions)

    def max_reply_chars(self, default=40):
        return self._max_reply_chars

    def resolve_audio(self, item, emotion=""):
        self.audio_queries.append((item.get("id"), emotion))
        return self._audio.get(item.get("id"), "")


def decision(**over) -> str:
    """按契约默认值拼一份 JSON，只覆盖关心的字段。"""
    base = {"should_reply": True, "priority": 1, "interrupt": False,
            "tone": "热情", "emotion": "happy", "speed": 1.0,
            "reason": "常规互动", "reply_text": "收到", "memory_note": ""}
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


def scripted_responder(system, user) -> str:
    """按事件内容给决策：把契约里的每个字段都走出不同取值，便于断言。"""
    if "嘉年华" in user:
        return decision(priority=0, interrupt=True, tone="激动", emotion="surprised",
                        speed=1.15, reason="大额礼物必须立刻谢",
                        reply_text="哇！谢谢老板的嘉年华！", memory_note="本场收到一个嘉年华")
    if "玫瑰" in user:
        return decision(priority=0, tone="热情", reason="中额礼物值得单独谢",
                        reply_text="谢谢老板的玫瑰")
    if "小心意" in user:
        return decision(reason="小额礼物连刷，攒一起谢", reply_text="谢谢甲哥的小心意")
    if "关注了直播间" in user:
        return decision(tone="亲切", reason="新观众关注，欢迎一下",
                        reply_text="谢谢关注，常来玩")
    if "粉丝团" in user:
        return decision(reason="新粉丝团成员，欢迎", reply_text="欢迎加入粉丝团！")
    if "进入了直播间" in user:
        return decision(should_reply=False, reason="进场太频繁，不必逐条欢迎",
                        reply_text="")
    if "666" in user:
        return decision(should_reply=False, reason="无意义刷屏，不回", reply_text="")
    if "怎么下载" in user:
        # 原样采纳 s1 的话术 → 应复用其预录音频（V4.7）
        return decision(emotion="calm", speed=0.95, reason="素材里有现成答案",
                        reply_text="右下角小手柄点一下就能下载", memory_note="答过下载")
    if "手机能玩" in user:
        return decision(reason="问机型适配", reply_text="手机能玩，进游戏挂着就行")
    if "回来了" in user:
        return decision(priority=2, emotion="得意", reason="闲时搭一句",
                        reply_text="回来啦兄弟")
    if "什么游戏" in user:
        return decision(reason="新人问游戏", reply_text="休闲挂机小游戏，点点小手柄下载")
    return decision(should_reply=False, reason="兜底不回", reply_text="")


class Session:
    """一次整场重放的产物集合。"""

    def __init__(self, router, brain, queue, tasks, selector, source):
        self.router = router
        self.brain = brain
        self.queue = queue
        self.tasks = tasks
        self.selector = selector
        self.source = source

    @property
    def records(self) -> list:
        """决策日志（按落盘顺序）。"""
        path = self.brain.decision_log.path()
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def by_text(self, text: str):
        for t in self.tasks:
            if t.text == text:
                return t
        raise AssertionError(f"没有产出文本为 {text!r} 的任务；实际：{[t.text for t in self.tasks]}")

    def playback_order(self) -> list:
        """按队列出队顺序取出全部任务（含优先级与插队效果的实证）。"""
        order = []
        while self.queue.qstat()["total"]:
            order.append(self.queue._pop())
        return order


async def run_session(tmp_path, responder=scripted_responder, replay_file=None,
                      speed=1000.0, **selector_kw):
    """整场重放：真实 Router + 真实队列 + AgentBrain + 桩 LLM。"""
    queue = PrioritySpeakQueue(config=CFG, no_expire=True)     # 与 main 的 agent 装配一致
    tasks = []

    async def on_task(task):
        tasks.append(task)
        await queue.put(task)

    router = Router(on_task, config=CFG, mode=MODE_AGENT)
    selector = StubSelector(**selector_kw)
    brain = AgentBrain(config=CFG, selector=selector, llm=StubLLM(responder),
                       decision_log=DecisionLogger(log_dir=tmp_path, enabled=True))
    router.set_agent_handler(brain.handle)

    source = ReplaySource(str(replay_file or REPLAY_FILE), router.handle_event, speed=speed)
    await source.run()
    await router.drain()
    return Session(router, brain, queue, tasks, selector, source)


# ================================================================ A. main 装配接线

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
    monkeypatch.setattr("hub.router.Router", RecordingRouter)
    monkeypatch.setattr("hub.webui.WebUi", RecordingWebUi)
    # main 在函数内 `from hub.queue import PrioritySpeakQueue`，补丁打到该模块属性上
    monkeypatch.setattr("hub.queue.PrioritySpeakQueue", RecordingQueue)
    return RecordingWebUi.captured


MAIN_CONFIG_YAML = """\
# 顶注
app:
  log_level: info
danmaku:
  ws_listen: 127.0.0.1:0

agent:
  enabled: false        # 总开关
"""


def run_startup(tmp_path, agent_enabled, llm=True):
    cfg = {
        "app": {"log_level": "info"},
        "danmaku": {"ws_listen": "127.0.0.1:0"},
        "agent": {"enabled": agent_enabled},
        "_config_path": str(tmp_path / "config.yaml"),
    }
    if llm:
        cfg["llm"] = {"base_url": "https://example.com/v1", "api_key": "sk-test",
                      "model": "main-model", "timeout_seconds": 8}
    (tmp_path / "config.yaml").write_text(MAIN_CONFIG_YAML, encoding="utf-8")

    stats = StatsCollector()
    got = {}

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(main_mod.run_main_loop(cfg, stats, stop))
        await asyncio.sleep(0.05)
        got["router"] = RecordingRouter.last
        got["queue"] = RecordingQueue.last
        got["stats"] = stats
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    got["cfg"] = cfg
    return got


def test_startup_agent_on_attaches_handler(tmp_path, record):
    """总开关 ON：启动即注入接管钩子，状态 provider 报「真在接管」。"""
    got = run_startup(tmp_path, agent_enabled=True)
    router = got["router"]
    assert router.mode == "agent"
    assert router.agent_attached is True

    st = record["agent_status_provider"]()
    assert st["enabled"] is True                    # 双条件成立
    assert st["model"] == "main-model"              # M5 状态条要用的模型名
    assert st["session"] == {"events": 0, "spoken": 0, "notes": 0}


def test_startup_agent_off_leaves_handler_detached(tmp_path, record):
    """出厂 OFF：不建 brain、不挂钩子——与旧版链路完全一致。"""
    got = run_startup(tmp_path, agent_enabled=False)
    assert got["router"].mode == "rules"
    assert got["router"].agent_attached is False

    st = record["agent_status_provider"]()
    assert st["enabled"] is False
    assert st["model"] == "" and st["session"] == {}


def test_startup_without_llm_key_never_attaches(tmp_path, record):
    """配置开了但没配 Key（V3.3 降级）：以规则管道跑，钩子不挂。"""
    got = run_startup(tmp_path, agent_enabled=True, llm=False)
    assert got["router"].mode == "rules"
    assert got["router"].agent_attached is False

    st = record["agent_status_provider"]()
    assert st["enabled"] is False
    assert st["configured_enabled"] is True and st["downgraded"] is True


def test_hot_switch_attaches_and_detaches(tmp_path, record):
    """热切换：开关一按，钩子与 mode 同步挂 / 摘，无需重启。"""
    got = run_startup(tmp_path, agent_enabled=False)
    switcher = record["agent_switcher"]
    router = got["router"]

    ok, message = asyncio.run(switcher(True))
    assert ok is True and "接管" in message
    assert (router.mode, router.agent_attached) == ("agent", True)
    assert record["agent_status_provider"]()["enabled"] is True

    ok, message = asyncio.run(switcher(False))
    assert ok is True
    assert (router.mode, router.agent_attached) == ("rules", False)
    assert record["agent_status_provider"]()["enabled"] is False


# ================================================================ B. 整场重放

def test_replay_session_every_event_gets_a_complete_decision(tmp_path):
    """V4.1 + V4.5：一场 14 条事件 → 14 条决策，字段齐全且行行可解析。"""
    session = asyncio.run(run_session(tmp_path))

    assert session.source.stats()["total_events"] == 14
    records = session.records
    assert len(records) == 14

    assert [r["type"] for r in records].count("chat") == 6
    assert [r["type"] for r in records].count("gift") == 5
    assert [r["type"] for r in records].count("follow") == 1
    assert [r["type"] for r in records].count("fansclub") == 1
    assert [r["type"] for r in records].count("enter") == 1

    contract = {"should_reply", "priority", "interrupt", "tone", "emotion", "speed",
                "reason", "reply_text", "memory_note"}
    for r in records:
        assert set(r) >= contract | {"type", "user", "event_line", "latency_ms",
                                     "model", "recall", "prerecorded", "ts"}
        assert r["model"] == "stub-model"
        assert r["latency_ms"] >= 0
        assert isinstance(r["priority"], int) and 0 <= r["priority"] <= 2
        assert r["event_line"]                      # 每条都有人话描述（M5 决策透视要展示）


def test_replay_decisions_land_on_tasks_with_priority(tmp_path):
    """V4.1 的下游：决策的优先级 / 插队 / 情绪 / 语速真的落到播放任务上。"""
    session = asyncio.run(run_session(tmp_path))

    # 14 条事件里 666 那条与进场那条决策为不回 → 12 条任务
    assert len(session.tasks) == 12
    assert all(t.source != "enter" for t in session.tasks)

    roc = session.by_text("哇！谢谢老板的嘉年华！")
    assert (roc.source, roc.priority, roc.interrupt) == ("gift", 0, True)
    assert roc.emotion == "surprised" and roc.speed == 1.15

    rose = session.by_text("谢谢老板的玫瑰")
    assert (rose.priority, rose.interrupt) == (0, False)      # 大额/中额都 P0，但没让插队

    assert session.by_text("谢谢甲哥的小心意").priority == 1
    assert session.by_text("欢迎加入粉丝团！").source == "fansclub"
    assert session.by_text("回来啦兄弟").priority == 2        # 闲时口播

    # 队列出队顺序实证：插队的嘉年华第一个播，P0 全部先于 P1
    order = session.playback_order()
    assert order[0].text == "哇！谢谢老板的嘉年华！"
    priorities = [t.priority for t in order]
    assert priorities == sorted(priorities)


def test_replay_prompt_carries_human_readable_memory(tmp_path):
    """提示词里的记忆段是人话行，不是事件 dict 的 repr（真实整场重放口径）。

    回归的是个只在"记忆对象 → 提示词"这个接缝上才暴露的问题：提示词单测传的是
    手搓的字符串列表，够绿；真实链路传的是 `SessionMemory.brief()`，于是 system
    prompt 里出现 `· {'type': 'chat', 'user': '观众甲', 'ts': 1790049318.18}`。
    """
    session = asyncio.run(run_session(tmp_path))
    calls = session.brain._llm.calls
    assert len(calls) == 14                        # 每事件一次调用，不多不少

    first = calls[0]["system"]
    assert "【本场直播记忆】" in first and "最近事件：" in first
    assert "弹幕 观众甲：主播这是什么游戏" in first
    assert "【本轮素材召回】" in first and "【可用情绪档位】" in first
    assert "【输出要求】" in first

    last = calls[-1]["system"]
    assert "弹幕 观众辛：主播回来了吗" in last      # 12 行窗口里能看到当前这条

    for system in (first, last):
        for noise in ("{'type'", "'gift_value':", "'ts':"):
            assert noise not in system, noise



    """V4.3 + V4.6：主动不回 → 不产出任务，但事件仍进单场记忆。"""
    session = asyncio.run(run_session(tmp_path))
    records = session.records

    no_reply = [r for r in records if not r["should_reply"]]
    assert {r["user"] for r in no_reply} == {"观众丙", "观众戊"}
    assert all(r["reply_text"] == "" for r in no_reply)       # 契约层就掐掉了可播文本

    texts = [t.text for t in session.tasks]
    assert "666" not in texts and not any("进入" in t for t in texts)

    events = session.brain.memory.events()
    assert len(events) == 14                                  # 不回的两条也在
    assert any(e["content"] == "666" for e in events)


def test_replay_memory_tracks_combo_and_spoken(tmp_path):
    """V4.6：同一用户连刷礼物在记忆里看得到；说过的进防复读缓冲与笔记。"""
    session = asyncio.run(run_session(tmp_path))
    memory = session.brain.memory

    gifts = [e for e in memory.events() if e["type"] == "gift"]
    assert [e["user"] for e in gifts] == ["观众甲", "观众甲", "观众甲", "观众丁", "观众戊"]
    assert [e["gift_count"] for e in gifts[:3]] == [1, 2, 3]   # 连刷的累计数看得见

    assert "哇！谢谢老板的嘉年华！" in memory.spoken()
    assert memory.is_repeated("哇！谢谢老板的嘉年华！") is True  # 再说一遍就会被判复读
    assert "本场收到一个嘉年华" in memory.notes()
    # 14 条事件里 2 条主动不回 → 12 句进防复读缓冲；笔记 2 条（嘉年华 / 答过下载）
    assert memory.stats() == {"events": 14, "spoken": 12, "notes": 2}


def test_replay_emotion_three_way_disposal(tmp_path):
    """V4.4：档位内 / 8 枚举内未勾选 / 非法 三种输入在真实重放里各归各位。"""
    session = asyncio.run(run_session(tmp_path))
    by_user = {r["user"]: r for r in session.records if r["type"] == "chat"}

    # ① 档位内（存档勾了 calm）→ 原样接受，不需要现场回落
    calm = by_user["观众己"]
    assert (calm["emotion_raw"], calm["emotion"], calm["emotion_event"]) == ("calm", "calm", "")
    assert session.by_text("右下角小手柄点一下就能下载").emotion == "calm"

    # ② 8 枚举内但存档没勾（surprised，出现在嘉年华那条礼物上）→ 接受并标记走现场 TTS
    roc = [r for r in session.records if r["type"] == "gift" and r["user"] == "观众戊"][0]
    assert (roc["emotion_raw"], roc["emotion"]) == ("surprised", "surprised")
    assert roc["emotion_event"] == "emotion_onsite"

    # ③ 非法档位（"得意" 不在 8 枚举内）→ 回落 default 并记明原因
    illegal = by_user["观众辛"]
    assert (illegal["emotion_raw"], illegal["emotion"]) == ("得意", "default")
    assert illegal["emotion_event"] == "emotion_invalid"
    assert session.by_text("回来啦兄弟").emotion == "default"


def test_replay_prerecorded_audio_reused_only_for_verbatim_recall(tmp_path):
    """V4.7：原样采纳召回素材 → 播它的预录音频；自己另写一句 → 不误用。"""
    session = asyncio.run(run_session(tmp_path))

    hit = session.by_text("右下角小手柄点一下就能下载")
    assert hit.audio_path == "C:/audio/s1.mp3"                # s1 有预录

    fresh = session.by_text("手机能玩，进游戏挂着就行")        # 与 s2 文案一致但 s2 无音频
    assert fresh.audio_path == ""
    assert [t.audio_path for t in session.tasks if t.audio_path] == ["C:/audio/s1.mp3"]

    logged = [r for r in session.records if r["prerecorded"]]
    assert len(logged) == 1 and logged[0]["user"] == "观众己"
    assert "sample:s1" in logged[0]["recall"]


# ================================================================ V4.2 非法输出

def test_illegal_outputs_never_crash_and_never_go_silent(tmp_path):
    """V4.2：三种非法形态各有着落——能被救回来的救，救不回来的走兜底口播。"""
    replies = [
        '{"should_reply": true, "priority": 1, "reply_text": "半截的回复", "memory',  # 截断
        '好的，我的判断如下：{"should_reply": true, "priority": 1, "reply_text": "混了说明文字"}',  # 混入
        "这个我随便说一句吧",                                                        # 纯散文
        '{"should_reply": true, "reply_',                                            # 残骸，救不回来
    ]
    answers = list(replies)

    def responder(system, user):
        return answers.pop(0)

    async def scenario():
        tasks = []
        queue = PrioritySpeakQueue(config=CFG, no_expire=True)

        async def on_task(task):
            tasks.append(task)
            await queue.put(task)

        router = Router(on_task, config=CFG, mode=MODE_AGENT)
        brain = AgentBrain(config=CFG, selector=StubSelector(), llm=StubLLM(responder),
                           decision_log=DecisionLogger(log_dir=tmp_path, enabled=True))
        router.set_agent_handler(brain.handle)
        for i, content in enumerate(["截断测试", "混入测试", "散文测试", "残骸测试"]):
            router.handle_event(LiveEvent(type="chat", user="观众", content=content, ts=float(i)))
        await router.drain()
        return tasks, brain

    tasks, brain = asyncio.run(scenario())

    assert [t.text for t in tasks] == [
        "半截的回复",              # 截断 → salvage
        "混了说明文字",            # 混入说明文字 → extract
        "这个我随便说一句吧",       # 纯散文 → plain
        "哈哈这个咱们直播间聊哈",   # 残骸 → DecisionError → Router 兜底口播
    ]
    assert all(t.priority == 1 for t in tasks)        # 降级与兜底都按 P1
    assert not any("should_reply" in t.text for t in tasks)   # 残骸绝不念给观众听

    degrade = [r["degrade"] for r in
               (json.loads(line) for line in
                brain.decision_log.path().read_text(encoding="utf-8").splitlines())]
    # 残骸那条在 brain 里就抛了，不落日志；前三条各自留下降级标记
    assert degrade == ["salvage", "extract", "plain"]


def test_llm_failure_falls_back_to_configured_reply(tmp_path):
    """第 3 级容错：LLM 调用异常 → `safety.fallback_reply`，且不插队。"""
    from hub.agent.llm import AgentLLMError

    class BoomLLM:
        model = "stub-model"

        async def ask(self, system, user):
            raise AgentLLMError("连接超时")

    async def scenario():
        tasks = []
        router = Router(lambda t: tasks.append(t), config=CFG, mode=MODE_AGENT)
        brain = AgentBrain(config=CFG, selector=StubSelector(), llm=BoomLLM(),
                           decision_log=DecisionLogger(log_dir=tmp_path, enabled=True))
        router.set_agent_handler(brain.handle)
        router.handle_event(LiveEvent(type="chat", user="观众", content="在吗", ts=1.0))
        await router.drain()
        return tasks

    tasks = asyncio.run(scenario())
    assert len(tasks) == 1
    assert tasks[0].text == "哈哈这个咱们直播间聊哈"
    assert tasks[0].priority == 1 and tasks[0].interrupt is False


def test_fallback_disabled_stays_silent_on_failure(tmp_path):
    """`safety.fallback_enabled: false` 是主播明确要求异常时闭嘴。"""
    from hub.agent.llm import AgentLLMError

    class BoomLLM:
        model = "stub-model"

        async def ask(self, system, user):
            raise AgentLLMError("连接超时")

    cfg = dict(CFG)
    cfg["safety"] = {"fallback_reply": "x", "fallback_enabled": False}

    async def scenario():
        tasks = []
        router = Router(lambda t: tasks.append(t), config=cfg, mode=MODE_AGENT)
        brain = AgentBrain(config=cfg, selector=StubSelector(), llm=BoomLLM(),
                           decision_log=DecisionLogger(log_dir=tmp_path, enabled=True))
        router.set_agent_handler(brain.handle)
        router.handle_event(LiveEvent(type="chat", user="观众", content="在吗", ts=1.0))
        await router.drain()
        return tasks

    assert asyncio.run(scenario()) == []
