"""F9 全局热键（PROMPTS 步骤 4.4，DESIGN §4.5；PRD FR-5）。

pynput 监听线程回调 → loop.call_soon_threadsafe 投递到事件循环 →
调度异步四件套；不得在监听线程内直接操作事件循环状态。

暂停语义（严格 PRD FR-5，立即生效）：
Router.set_paused（停止产出新任务，事件照收只记录）→
Queue.pause（清空积压含 P0、挂起 get）→ Player.pause（立即中断当前句）
→ 控制台醒目打印 [已暂停发声] 并写日志。恢复为反向四件套（不补播被中断的句）。

热键监听本身不进单测（全局键盘钩子不适合自动化）：测试注入 on_hotkey()
直接触发；pynput 不可用时 start() 返回 False，主入口醒目告警并以
Ctrl+C 完全停止作为人工兜底（RUNBOOK 记载）。
"""
import asyncio

from hub.logger import get_logger

_DEFAULT_NOTIFY = lambda msg: print(msg)  # noqa: E731


class HotkeyController:
    def __init__(self, router, queue, player=None, config: dict = None,
                 stats=None, log=None, notify=None, loop=None):
        cfg = (config or {}).get("safety", {}) or {}
        self._hotkey = str(cfg.get("pause_hotkey", "f9") or "f9").strip().lower()
        self._router = router
        self._queue = queue
        self._player = player
        self._stats = stats
        self._log = log or get_logger("hotkey")
        self._notify = notify or _DEFAULT_NOTIFY
        self._loop = loop
        self._listener = None
        self._paused = False

    # ---------- 对外接口 ----------

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def hotkey_name(self) -> str:
        return self._hotkey

    @property
    def player(self):
        return self._player

    @player.setter
    def player(self, value) -> None:
        """热替换播放器（音色工坊配置 Key 后从打印模式升级为 Player）。"""
        self._player = value

    def on_hotkey(self) -> None:
        """热键触发入口（pynput 监听线程调用）：线程安全投递到事件循环。"""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._toggle)
        except RuntimeError:
            pass  # 事件循环已关闭（关停中），忽略

    def _toggle(self) -> None:
        """事件循环线程内执行：按当前状态取反，顺序调度（连按按序生效）。"""
        asyncio.ensure_future(self._apply(not self._paused))

    async def _apply(self, pause: bool) -> None:
        if pause == self._paused:
            return
        self._paused = pause
        extra = {"hotkey": self._hotkey, "player": self._player is not None}
        try:
            self._router.set_paused(pause)
            if pause:
                await self._queue.pause()
                if self._player is not None:
                    await self._player.pause()
                self._notify(f"[已暂停发声]  立即停止，再按 {self._hotkey.upper()} 恢复")
                self._log.info("hotkey_paused", extra)
            else:
                if self._player is not None:
                    await self._player.resume()
                await self._queue.resume()
                self._notify("[已恢复发声]  等待新的弹幕或话题")
                self._log.info("hotkey_resumed", extra)
        except Exception as e:
            self._log.error("hotkey_toggle_failed", {
                "pause": pause, "error": f"{type(e).__name__}: {e}"})
            self._notify(f"[热键异常] {'暂停' if pause else '恢复'}未完全生效：{e}，必要时 Ctrl+C 退出")

    async def toggle(self) -> None:
        """测试与程序化触发入口（事件循环线程内调用）。"""
        await self._apply(not self._paused)

    # ---------- 监听生命周期 ----------

    def start(self) -> bool:
        """启动 pynput 全局监听。失败返回 False（调用方告警并退回 Ctrl+C 兜底）。"""
        if self._loop is None:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._log.error("hotkey_start_no_loop")
                return False
        try:
            from pynput import keyboard
        except Exception as e:
            self._log.warning("hotkey_unavailable", {"error": f"{type(e).__name__}: {e}"})
            return False

        def on_press(key):
            try:
                if self._matches(key, keyboard):
                    self.on_hotkey()
            except Exception:
                pass

        try:
            self._listener = keyboard.Listener(on_press=on_press)
            self._listener.start()
        except Exception as e:
            self._listener = None
            self._log.warning("hotkey_start_failed", {"error": f"{type(e).__name__}: {e}"})
            return False
        self._log.info("hotkey_started", {"hotkey": self._hotkey})
        return True

    def _matches(self, key, keyboard) -> bool:
        name = self._hotkey
        if len(name) == 1:  # 单字符键（如 p）：对应 KeyCode.char
            return getattr(key, "char", None) == name
        return key == getattr(keyboard.Key, name, None)

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
