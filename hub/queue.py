"""优先级播放队列（PROMPTS 步骤 2.5，DESIGN §4.2/§6.3；PRD FR-1/FR-2）。

三级分层：P0 礼物感谢（可插队头）、P1 互动回复、P2 定时口播。
出队年龄检查：过期任务丢弃并计数（弹幕风暴下的自然限流）——P1 弹幕回复走
stale_drop_p1_s（默认 90s：LLM 生成+积压常超 30s，观众语境 90s 内仍有效），
P2 话题口播走 stale_drop_p2_s（默认 180s：P2 让位积压上限 1 条，需挨过当前
在播的最长话题），P0 礼物走 stale_drop_s（默认 30s：礼物感谢要趁热）。
让位规则：put_idle（P2 口播）入队前检查任意层级积压（含 P2 自身），有则放弃
本轮（步骤 5.1 使用）——P2 不自查时，口播间隔小于单条播放时长会无限积压，
旧话题批量过期、同一条话术几分钟内反复播（听起来像「话术说两遍」）。
暂停语义：pause() 清空全部积压（含 P0——恢复后语境已过期）并挂起 get()，供步骤 4.4 热键接线。

智能体模式（M3 / DESIGN §8.1）：总开关打开时置 `no_expire`——
出队不再丢过期任务（agent 的决策成本高，随口一说也算素材，且它自己按语境
判断该不该说）；`put_idle` 不再自查积压（agent 模式下 P2 口播不是"空闲才说"
的轮播，而是智能体决策的一个来源）。优先级排序与 interrupt 头插在任何模式
下都保留。开关置回 OFF 即完全恢复旧行为（V3.6）。
"""
import asyncio
import time
from collections import deque

from hub.events import SpeakTask
from hub.logger import get_logger


class PrioritySpeakQueue:
    def __init__(self, config: dict = None, stats=None, clock=None, log=None,
                 no_expire: bool = False):
        cfg = config or {}
        events = cfg.get("events", {}) or {}
        chat_cfg = events.get("chat", {}) or {}
        self._stale_drop_s = float(chat_cfg.get("stale_drop_s", 30))
        self._stale_drop_p1_s = float(chat_cfg.get("stale_drop_p1_s", 90))
        self._stale_drop_p2_s = float(chat_cfg.get("stale_drop_p2_s", 180))
        self._no_expire = bool(no_expire)

        self._p0: deque[SpeakTask] = deque()
        self._p1: deque[SpeakTask] = deque()
        self._p2: deque[SpeakTask] = deque()
        self._cond = asyncio.Condition()
        self._paused = False
        self._stats = stats
        self._clock = clock or time.time
        self._log = log or get_logger("queue")

    # ---------- 入队 ----------

    async def put(self, task: SpeakTask) -> None:
        """常规入队：interrupt 标记的 P0 头插（插队），其余按优先级尾插。"""
        async with self._cond:
            self._append(task, head=bool(task.interrupt and task.priority == 0))
            self._cond.notify()

    async def put_head(self, task: SpeakTask) -> None:
        """头插入自身优先级层（显式插队入口）。"""
        async with self._cond:
            self._append(task, head=True)
            self._cond.notify()

    async def put_idle(self, task: SpeakTask) -> bool:
        """P2 口播专用（步骤 5.1 定时口播调用）。

        任意层级存在积压（含 P2 自身）时放弃本次入队并返回 False，调用方等下
        一轮——空转不占队，实现"空闲才说话"。P2 自查是关键：口播间隔若小于
        单条播放时长，不自查会让 P2 无限积压，旧话题批量过期、同一话题在
        几分钟内反复播（听起来像「话术说两遍」）；自查后积压上限 1 条，
        轮播节奏自动对齐真实播放速度。

        no_expire（agent 模式）下不查积压：此时 P2 口播只是智能体可选的说话
        时机之一，判断交给智能体，队列不再替它让位。
        """
        async with self._cond:
            if not self._no_expire and (self._p0 or self._p1 or self._p2):
                self._log.debug("idle_task_yielded", {"source": task.source})
                return False
            self._append(task, head=False)
            self._cond.notify()
            return True

    def _append(self, task: SpeakTask, head: bool) -> None:
        dq = {0: self._p0, 1: self._p1}.get(task.priority, self._p2)
        if head:
            dq.appendleft(task)
        else:
            dq.append(task)

    # ---------- 出队 ----------

    async def get(self) -> SpeakTask:
        """出队：P0 → P1 → P2；过期任务丢弃继续取；暂停时挂起。"""
        while True:
            async with self._cond:
                while self._paused or not (self._p0 or self._p1 or self._p2):
                    await self._cond.wait()
                task = self._pop()
                if self._is_stale(task):
                    if self._stats is not None:
                        self._stats.inc("dropped_stale")
                    self._log.info("task_dropped_stale", {
                        "source": task.source, "age_s": round(self._clock() - task.event_ts, 1),
                    })
                    continue
                return task

    def _pop(self) -> SpeakTask:
        if self._p0:
            return self._p0.popleft()
        if self._p1:
            return self._p1.popleft()
        return self._p2.popleft()

    def _is_stale(self, task: SpeakTask) -> bool:
        """出队年龄检查。agent 模式（no_expire）恒 False —— 不丢任何任务（V3.5）。"""
        if self._no_expire:
            return False
        if task.priority == 1:
            limit = self._stale_drop_p1_s
        elif task.priority == 2:
            limit = self._stale_drop_p2_s
        else:
            limit = self._stale_drop_s
        return (self._clock() - task.event_ts) > limit

    # ---------- 智能体模式（M3） ----------

    @property
    def no_expire(self) -> bool:
        return self._no_expire

    def set_no_expire(self, value: bool) -> None:
        """热切换开关（V3.2）：关掉后 _is_stale 立即恢复旧行为（V3.6）。

        已在队里的任务不因本次切换被清理——语义切换只影响后续出队判定。
        """
        value = bool(value)
        if value == self._no_expire:
            return
        self._no_expire = value
        self._log.info("queue_no_expire_changed", {"no_expire": value})

    # ---------- 暂停与清理 ----------

    def clear(self) -> int:
        """清空全部积压（含 P0），返回清理条数（计入 dropped_pause 供 stats 自洽）。"""
        n = len(self._p0) + len(self._p1) + len(self._p2)
        self._p0.clear()
        self._p1.clear()
        self._p2.clear()
        if n:
            if self._stats is not None:
                self._stats.inc("dropped_pause", n)
            self._log.info("queue_cleared", {"cleared": n})
        return n

    async def pause(self) -> None:
        """暂停：清空积压并让 get() 挂起（PRD FR-5 语义）。"""
        async with self._cond:
            self._paused = True
            self.clear()
            self._cond.notify_all()
        self._log.info("queue_paused")

    async def resume(self) -> None:
        async with self._cond:
            self._paused = False
            self._cond.notify_all()
        self._log.info("queue_resumed")

    @property
    def paused(self) -> bool:
        return self._paused

    def qstat(self) -> dict:
        """各优先级积压数（P2 让位判断与启动自检用）。"""
        return {"p0": len(self._p0), "p1": len(self._p1), "p2": len(self._p2),
                "total": len(self._p0) + len(self._p1) + len(self._p2)}
