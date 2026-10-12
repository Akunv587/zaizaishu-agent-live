"""试玩调试（DESIGN §7.2②，M5）：在控制台手工造一条事件，看智能体怎么决策。

**隔离是硬约束（V5.3）**——试玩与直播是两条互不干扰的路径：

| 状态 | 直播决策（`handle`） | 试玩（`preview`） |
|---|---|---|
| 单场记忆 `events` / `spoken` / `notes` | 写 | **不写** |
| 检索层去重窗口（"刚说过别再召回"） | 写 | **不写** |
| 决策日志 `logs/agent-*.jsonl` | 写 | **不写** |
| 播放队列 | 产出 `SpeakTask` | **不产出** |
| 人设素材 / 单场记忆（读） | 读 | 读（要看的正是"此刻它会怎么回"） |

所以"试玩期间直播链路零影响"是**可实测**的：试玩前后比对 `AgentBrain.memory.stats()`
与 `PrioritySpeakQueue.qstat()`，逐字段相等（见 `tests/test_agent_playground.py`）。
决策日志也不写——否则"本场决策数"的聚合卡会把试玩算进去，那是自欺。

**为什么不让试玩也进队列**：试玩是"预演"，用户要看的是判断结果本身；真投递会让
虚拟声卡出声、打断真实直播口播，那就不是试玩了。

批量上限 `MAX_BATCH = 20`：一次投 100 条会打 100 次 LLM 请求（含 system prompt），
既打上游配额又让页面滚不到底。要压测请用脚本，不要在页面上按按钮。
"""
from hub.agent.prompt import describe_event
from hub.logger import get_logger
from hub.router import gift_tier

# 事件类型（与 hub.events.METHOD_TO_TYPE 的值域一致）
EVENT_TYPES = ("chat", "gift", "follow", "fansclub", "enter")
# 一次批量投递的条数上限（见模块头说明）
MAX_BATCH = 20
MAX_USER_CHARS = 40
MAX_CONTENT_CHARS = 200
MAX_GIFT_NAME_CHARS = 40
# 礼物价值上限（防手滑输入 999999999 把 prompt 与档位搞乱）
MAX_GIFT_VALUE = 1_000_000


class PlaygroundError(Exception):
    """试玩输入非法（页面表单问题）——由调用方回显给用户，不进决策链路。"""


def _text(value, limit: int, field: str, required: bool = False) -> str:
    s = str(value if value is not None else "").strip().replace("\n", " ")
    if required and not s:
        raise PlaygroundError(f"{field}不能为空")
    return s[:limit]


def _int(value, default: int, lo: int, hi: int, field: str) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        raise PlaygroundError(f"{field}必须是数字") from None
    if n < lo or n > hi:
        raise PlaygroundError(f"{field}需在 {lo} ~ {hi} 之间")
    return n


def build_context(raw: dict) -> dict:
    """页面表单 → Router 同形状的事件上下文。

    形状与 `Router._event_context` / `_single_context` 对齐，这样试玩看到的决策
    与直播看到的决策是同一份输入口径（含 `tier` 的档位标注，V5.1 要看它）。
    """
    raw = raw or {}
    kind = str(raw.get("type") or "").strip().lower()
    if kind not in EVENT_TYPES:
        raise PlaygroundError(f"事件类型只能是 {' / '.join(EVENT_TYPES)}")
    user = _text(raw.get("user"), MAX_USER_CHARS, "观众昵称", required=True)

    ctx = {
        "type": kind,
        "user": user,
        "content": _text(raw.get("content"), MAX_CONTENT_CHARS, "内容"),
        "merged_count": 1,
        # members 供 describe_event 渲染弹幕行；非弹幕类型留空（与 Router 一致）
        "members": [],
        "event_ts": 0.0,      # 由调用方补当前时刻（试玩不参与过期判定）
        "test": True,         # 试玩一律标记为测试事件：日志/聚合卡据此区分
    }
    if kind == "chat":
        if not ctx["content"]:
            raise PlaygroundError("弹幕内容不能为空")
        ctx["members"] = [(user, ctx["content"])]
    elif kind == "gift":
        name = _text(raw.get("gift_name"), MAX_GIFT_NAME_CHARS, "礼物名", required=True)
        count = _int(raw.get("gift_count"), 1, 1, 999, "礼物数量")
        value = _int(raw.get("gift_value"), 0, 0, MAX_GIFT_VALUE, "礼物价值")
        ctx.update({"gift_name": name, "gift_count": count,
                    "gift_value": value, "tier": gift_tier(value)})
    return ctx


class Playground:
    """试玩调度器：把一条/一批事件交给 `AgentBrain.preview`，汇总成页面能渲染的结构。"""

    def __init__(self, brain, clock=None, max_batch: int = MAX_BATCH, log=None):
        self._brain = brain
        self._clock = clock or (lambda: 0.0)
        self._max_batch = max(1, int(max_batch))
        self._log = log or get_logger("agent.playground")

    @property
    def available(self) -> bool:
        """能否试玩（brain 已装配）。未装配时页面置灰并说明原因。"""
        return self._brain is not None

    async def run(self, raw_events) -> dict:
        """逐条试玩，返回 `{"results": [...], "summary": {...}}`。

        - 输入非法（`PlaygroundError`）→ 整批拒绝：表单错了就当场说清楚，别跑一半；
        - 单条决策失败（LLM 异常 / 输出不可用 / 未配 Key）→ 记在该条的 `error` 上继续跑，
          批量投递不该因为一条失败就丢掉其余结果；
        - 顺序执行而非并发：一是要保持与直播一致的事件先后（记忆与召回的时序敏感性），
          二是并发 20 次 LLM 调用会把上游限流踩出来，页面看到的失败率反而是假的。
        """
        if self._brain is None:
            raise PlaygroundError("智能体未装配（未开启总开关或未配置 LLM Key），无法试玩")
        items = list(raw_events or [])
        if not items:
            raise PlaygroundError("请先构造至少一条事件")
        if len(items) > self._max_batch:
            raise PlaygroundError(f"一次最多试玩 {self._max_batch} 条（当前 {len(items)} 条）")

        # 先全部校验：一条不合法就整批拒绝，不产生"跑了前三条才发现第四条错"
        contexts = [build_context(it) for it in items]

        now = float(self._clock())
        results = []
        for index, ctx in enumerate(contexts):
            ctx["event_ts"] = now
            results.append(await self._one(index, ctx))
        return {"results": results, "summary": summarize(results)}

    async def _one(self, index: int, ctx: dict) -> dict:
        try:
            record = await self._brain.preview(ctx)
        except Exception as e:  # 单条失败不拖垮整批（含 AgentLLMError / DecisionError）
            message = f"{type(e).__name__}: {e}"
            self._log.warning("playground_decision_failed",
                              {"type": ctx.get("type"), "error": message})
            return {"index": index, "type": ctx["type"], "user": ctx["user"],
                    "event_line": describe_event(ctx), "error": message,
                    "should_reply": False}
        return {"index": index, "error": "", "event_line": describe_event(ctx), **record}


def summarize(results) -> dict:
    """批量试玩的汇总：回了多少 / 各类型分布 / 平均耗时 / 失败数。

    与"决策透视"的聚合卡口径刻意不同：那边统计**真实直播**（来自决策日志），
    这边统计**这一批试玩**。两者混在一起就没有意义了。
    """
    items = list(results or [])
    replied = [r for r in items if r.get("should_reply") and not r.get("error")]
    failed = [r for r in items if r.get("error")]
    latencies = [int(r.get("latency_ms") or 0) for r in items if not r.get("error")]
    by_type: dict = {}
    for r in items:
        kind = str(r.get("type") or "")
        by_type[kind] = by_type.get(kind, 0) + 1
    return {
        "count": len(items),
        "replied": len(replied),
        "no_reply": len(items) - len(replied) - len(failed),
        "failed": len(failed),
        "by_type": by_type,
        "avg_latency_ms": (int(round(sum(latencies) / len(latencies)))
                           if latencies else 0),
    }


def build_batch(kind: str, count: int, **fields) -> list:
    """一键造一批同类事件（模拟弹幕风暴用，页面"批量投递"按钮调它）。"""
    n = _int(count, 1, 1, MAX_BATCH, "批量条数")
    batch = []
    for i in range(1, n + 1):
        item = {"type": kind, **fields}
        if kind == "chat":
            content = _text(fields.get("content"), MAX_CONTENT_CHARS, "内容")
            item["content"] = content if n == 1 else f"{content}（第 {i} 条）"
        elif kind == "gift":
            item["gift_count"] = i if n > 1 else _int(
                fields.get("gift_count"), 1, 1, 999, "礼物数量")
        batch.append(item)
    return batch


__all__ = ["EVENT_TYPES", "MAX_BATCH", "Playground", "PlaygroundError",
           "build_batch", "build_context", "summarize"]
