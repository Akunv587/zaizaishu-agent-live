"""内置弹幕采集器真实连接验证（原生签名直连）。

用法：
    python scripts/douyin_probe.py <抖音房间号> [监听秒数，默认60]

验证链路：首页取 ttwid → web/enter 房间信息 → im/fetch 预取 → 原生签名
wss 直连 → protobuf 解码 → DyMessage 事件回调打印。退出码 0 = 连接成功
（live 状态）且收到过事件。

cookie：可选增强，读取 config.yaml 的 danmaku.cookie（真实登录态提升信任度）；
默认空——直连链路只依赖首页自取的 ttwid，无需用户导出 cookie。
"""
import asyncio
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.douyin import DouyinCollector
from hub.events import normalize
from hub.logger import setup


def _load_cookie() -> str:
    """读 config.yaml 的 danmaku.cookie（probe 独立运行，不走完整校验链）。"""
    cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return ""
    return str((cfg.get("danmaku") or {}).get("cookie", "") or "").strip()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    room_num = sys.argv[1]
    try:
        duration = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    except ValueError:
        duration = 60.0

    cookie = _load_cookie()
    if cookie:
        print(f"[cookie] 使用 config.yaml 粘贴的浏览器 cookie 增强（{len(cookie)} 字符）")
    else:
        print("[cookie] 未配置 danmaku.cookie，仅用首页 ttwid（原生签名直连足够）")

    setup(Path("logs"), "debug")
    counters: dict = {"chat": 0, "gift": 0, "follow": 0, "enter": 0}

    def on_dy(dy: dict) -> None:
        ev = normalize(dy)
        if ev is None:
            return
        counters[ev.type] = counters.get(ev.type, 0) + 1
        if ev.type == "gift":
            print(f"[礼物] {ev.user} 送 {ev.gift_name} x{ev.gift_count} "
                  f"(抖币{ev.gift_value} 连击结束={ev.combo_end})")
        elif ev.type == "chat":
            print(f"[弹幕] {ev.user}: {ev.content}")
        else:
            print(f"[{ev.type}] {ev.user} {ev.content}")

    async def run() -> int:
        stop = asyncio.Event()
        collector = DouyinCollector(room_num, on_dy, cookie=cookie)

        async def _timeout() -> None:
            await asyncio.sleep(duration)
            stop.set()

        timeout_task = asyncio.create_task(_timeout())
        try:
            await collector.run(stop)
        finally:
            timeout_task.cancel()

        s = collector.status()
        print("\n── 验证结果 ─────────────────────────────")
        print(f"房间: {room_num} → room_id={s['room_id']} "
              f"主播「{s['anchor']}」 标题「{s['title']}」")
        print(f"状态: {s['state']}  重连: {s['reconnects']}  消息帧: {s['messages']}")
        print(f"事件计数: {counters}")
        if s["last_error"]:
            print(f"最后错误: {s['last_error']}")
        print("─────────────────────────────────────────")
        total = sum(counters.values())
        return 0 if (total > 0 or s["state"] == "live") else 1

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
