"""仔仔鼠AI智能体直播助手 · 桌面壳。

一条命令启动：python desktop.py —— 后台线程跑完整中枢（main.run_main_loop，
与 python main.py 同一条代码路径），主线程弹原生窗口（WebView2）加载
可视化控制台。窗口标题「仔仔鼠AI智能体直播助手」，关闭时记住大小与位置
（desktop_state.json），下次启动原样恢复。

退出：关窗口即全链路优雅退出（采集器/播放/看门狗收尾与 main.py 一致）。
"""
import argparse
import asyncio
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hub.config import ConfigError, load_config  # noqa: E402
from hub.logger import setup as setup_logger  # noqa: E402
from hub.paths import BUNDLE_DIR, DEFAULT_CONFIG, EXE_DIR, FROZEN, LOGS_DIR  # noqa: E402
from hub.stats import StatsCollector  # noqa: E402

import main as main_mod  # noqa: E402

TITLE = "仔仔鼠AI智能体直播助手"
STATE_FILE = EXE_DIR / "desktop_state.json"
DEFAULT_SIZE = (1280, 860)
MIN_SIZE = (980, 640)
CONSOLE_READY_TIMEOUT_S = 25.0


def _seed_user_file(target: Path, template: Path) -> bool:
    """exe 旁缺用户数据文件时从内置模板补一份（返回是否新落地）。

    按字节复制：BOM 与换行风格原样保留（write_text 会做平台换行转换）。
    """
    if target.exists():
        return False
    try:
        target.write_bytes(template.read_bytes())
    except OSError as e:
        print(f"[桌面壳] {target.name} 模板落地失败: {e}")
        return False
    return True


def _prepare_config() -> tuple[str, bool]:
    """冻结模式首次运行：exe 旁缺 config.yaml / blockwords.txt 时从内置模板补齐。

    模板自带 LLM 占位值与起始词库，双击即进控制台（无需手工预配置）；
    LLM 密钥在控制台「配置 LLM」面板里填写并保存即热生效。
    源码运行原样返回默认路径。
    """
    if not FROZEN:
        return DEFAULT_CONFIG, False
    # 词库与配置各自独立补齐：删了其一不影响另一个
    _seed_user_file(EXE_DIR / "blockwords.txt", BUNDLE_DIR / "blockwords.example.txt")
    if not Path(DEFAULT_CONFIG).exists():
        _seed_user_file(Path(DEFAULT_CONFIG), BUNDLE_DIR / "config.example.yaml")
        return DEFAULT_CONFIG, True
    return DEFAULT_CONFIG, False


def _alert(text: str) -> None:
    """原生消息框（配置失败/控制台未起来时兜底提示）。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10)
    except Exception:
        print(text)


class _JsApi:
    """暴露给控制台页面的原生能力（window.pywebview.api.*）。

    choose_save_path：话术存档导出 zip 时弹系统「另存为」对话框自选位置；
    用户取消返回空串，前端据此放弃导出。
    """

    def __init__(self):
        self._window = None

    def choose_save_path(self, default_name: str = "archive.zip") -> str:
        if self._window is None:
            return ""
        try:
            import webview

            result = self._window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default_name or "archive.zip",
                file_types=("ZIP 压缩包 (*.zip)",))
        except Exception as e:
            print(f"[桌面壳] 另存为对话框失败: {e}")
            return ""
        if not result:
            return ""
        path = result if isinstance(result, str) else result[0]
        return str(path) if path else ""


def _load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(x: int, y: int, width: int, height: int) -> None:
    try:
        STATE_FILE.write_text(json.dumps(
            {"x": x, "y": y, "width": width, "height": height}),
            encoding="utf-8")
    except OSError as e:
        print(f"[桌面壳] 窗口状态保存失败: {e}")


def _window_kwargs() -> dict:
    """上次窗口几何 → create_window 参数（越界/未保存时用默认值）。"""
    state = _load_state()
    kwargs: dict = {"width": DEFAULT_SIZE[0], "height": DEFAULT_SIZE[1]}
    if isinstance(state.get("width"), int) and MIN_SIZE[0] <= state["width"] <= 7680:
        kwargs["width"] = state["width"]
    if isinstance(state.get("height"), int) and MIN_SIZE[1] <= state["height"] <= 4320:
        kwargs["height"] = state["height"]
    x, y = state.get("x"), state.get("y")
    if isinstance(x, int) and isinstance(y, int) and -2000 < x < 30000 and -2000 < y < 30000:
        kwargs["x"] = x
        kwargs["y"] = y
    return kwargs


def _hub_worker(cfg: dict, stats: StatsCollector, holder: dict,
                ready: threading.Event) -> None:
    """后台线程：独立事件循环跑中枢主循环，stop 事件暴露给窗口关闭。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    stop = asyncio.Event()
    holder["loop"] = loop
    holder["stop"] = stop
    ready.set()
    try:
        loop.run_until_complete(
            main_mod.run_main_loop(cfg, stats, stop=stop, open_browser=False))
    except Exception as e:  # 启动自检失败等：让主线程弹窗给用户看
        holder["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            loop.close()
        except Exception:
            pass


def _stop_hub(holder: dict) -> None:
    loop = holder.get("loop")
    stop = holder.get("stop")
    if loop is not None and stop is not None and not stop.is_set():
        loop.call_soon_threadsafe(stop.set)


def _wait_console(url: str, timeout_s: float) -> bool:
    """轮询控制台 HTTP 就绪（中枢装配含 LLM 提示词渲染等，给足时间）。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def main() -> int:
    default_config, first_run = _prepare_config()
    parser = argparse.ArgumentParser(description="仔仔鼠AI智能体直播助手 · 桌面版")
    parser.add_argument("--config", default=default_config,
                        help="配置文件路径（默认 exe 旁 config.yaml）")
    args = parser.parse_args()

    if first_run:
        # 无需先填 LLM 密钥（用户需求）：模板自带占位值可直接启动，
        # 控制台「配置 LLM」面板里填写并保存即热生效
        print("[桌面壳] 首次运行：已生成配置模板，LLM 密钥等可在控制台「配置 LLM」中填写（热生效）")

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        _alert(f"配置校验未通过：\n\n{e}")
        return 1

    webui_cfg = cfg.setdefault("webui", {})
    if not webui_cfg.get("enabled"):
        webui_cfg["enabled"] = True
        print("[桌面壳] config.yaml webui.enabled=false：桌面模式需要控制台，本次运行已自动启用")

    listen = str(webui_cfg.get("listen", "127.0.0.1:8766"))
    host, _, port = listen.rpartition(":")
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    url = f"http://{host}:{port or 8766}/"

    app_cfg = cfg.get("app", {})
    setup_logger(LOGS_DIR, app_cfg.get("log_level", "info"))
    main_mod.print_config_summary(cfg)
    print(f"[桌面壳] 窗口地址: {url}")

    stats = StatsCollector()
    holder: dict = {}
    ready = threading.Event()
    hub_thread = threading.Thread(
        target=_hub_worker, args=(cfg, stats, holder, ready),
        name="hub", daemon=True)
    hub_thread.start()
    ready.wait(timeout=5)

    if not _wait_console(url, CONSOLE_READY_TIMEOUT_S):
        error = holder.get("error", "")
        _alert(f"控制台服务启动失败{f'：\n{error}' if error else ''}，"
               f"请查看控制台输出与 logs/ 后重试")
        _stop_hub(holder)
        hub_thread.join(timeout=8)
        return 1

    import webview

    js_api = _JsApi()
    window = webview.create_window(TITLE, url, min_size=MIN_SIZE, js_api=js_api,
                                   **_window_kwargs())
    js_api._window = window
    geometry: dict = {}

    def _capture_geometry() -> None:
        try:
            geometry["x"] = window.x
            geometry["y"] = window.y
            geometry["width"] = window.width
            geometry["height"] = window.height
        except Exception:
            pass

    # closing：窗口销毁前仍可读几何；closed 时 WinForms 已回收（实测返回 None）
    window.events.closing += _capture_geometry
    webview.start()

    if not geometry:  # 兜底：closed 路径失败时再试一次
        _capture_geometry()
    x, y = geometry.get("x"), geometry.get("y")
    w, h = geometry.get("width"), geometry.get("height")
    # 最小化时 Windows 给 -32000：越界值不保存
    if all(isinstance(v, int) for v in (x, y, w, h)) and -2000 < x < 30000 and w >= MIN_SIZE[0]:
        _save_state(x, y, w, h)

    print("[桌面壳] 窗口已关闭，正在收尾退出 …")
    _stop_hub(holder)
    hub_thread.join(timeout=10)
    print("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
