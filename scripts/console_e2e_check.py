"""阶段1 验收：控制台贴房间号 → 实时弹幕流（端到端）。

流程：以无 room_num 的配置启动中枢（webui 独立端口，TTS 打印模式避免真实发声）
→ 浏览器 WS 客户端连接控制台 → 发送 {"action":"set_room"} → 等待真实弹幕事件
推送到控制台事件流。退出码 0 = 收到至少 1 条弹幕事件。
"""
import asyncio
import json
import socket
import sys
from pathlib import Path

import yaml
from websockets.asyncio.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main as main_mod  # noqa: E402
from hub.stats import StatsCollector  # noqa: E402

ROOM = sys.argv[1] if len(sys.argv) > 1 else "231946351964"
WAIT_S = float(sys.argv[2]) if len(sys.argv) > 2 else 40.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _load_cfg(port: int) -> dict:
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent
                          / "config.yaml").read_text(encoding="utf-8"))
    cfg["danmaku"]["room_num"] = ""          # 从外挂模式起步，靠 set_room 切换
    cfg["danmaku"]["cookie"] = ""
    cfg["tts"]["voice_id"] = ""               # TTS 退打印模式，验收不发声不耗配额
    cfg["webui"]["listen"] = f"127.0.0.1:{port}"
    cfg["schedule"]["enabled"] = False        # 口播调度静默，避免干扰观察
    return cfg


async def main() -> int:
    port = _free_port()
    cfg = _load_cfg(port)
    stats = StatsCollector()
    stop = asyncio.Event()
    hub_task = asyncio.create_task(main_mod.run_main_loop(cfg, stats, stop))

    danmaku_events: list[dict] = []
    try:
        await asyncio.sleep(1.5)  # 等中枢起来
        async with connect(f"ws://127.0.0.1:{port}/ws", open_timeout=5) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)  # snapshot
            await asyncio.wait_for(ws.recv(), timeout=5)  # feed
            print(f"[1] 控制台已连接，发送 set_room={ROOM}")
            await ws.send(json.dumps({"action": "set_room", "room": ROOM}))

            source_room = ""
            deadline = asyncio.get_event_loop().time() + WAIT_S
            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(1.0, deadline - asyncio.get_event_loop().time()))
                except asyncio.TimeoutError:
                    break
                msg = json.loads(raw)
                if msg["type"] == "snapshot":
                    src = msg.get("source") or {}
                    if src.get("room_num"):
                        source_room = src["room_num"]
                        state = src.get("state")
                        print(f"[2] 弹幕源 → 房间 {source_room} 状态 {state}"
                              f"{'（原生签名直连成功）' if state == 'live' else ''}")
                elif msg["type"] == "event":
                    ev = msg["data"]
                    if ev.get("kind") == "danmaku":
                        danmaku_events.append(ev)
                        print(f"[3] 弹幕: {ev.get('user')}: {ev.get('content')}")
                        if len(danmaku_events) >= 5:
                            break
    finally:
        stop.set()
        try:
            await asyncio.wait_for(hub_task, timeout=10)
        except Exception as e:
            print(f"[!] 中枢收尾异常: {type(e).__name__}: {e}")

    print("── 验收结果 ─────────────────────────")
    print(f"源房间: {source_room or '<未连接>'}  收到弹幕事件: {len(danmaku_events)}")
    ok = source_room == ROOM and danmaku_events
    print("通过" if ok else "失败")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
