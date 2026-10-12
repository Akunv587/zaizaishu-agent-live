"""模拟 Dycast 的 WS 客户端联调工具（线上模式验证）。

用途：工具以 python main.py 线上模式运行时，本脚本作为 WS 客户端连上
127.0.0.1:8765，发送几条 Dycast 格式的真实事件（弹幕/礼物/关注），
用于不开 Dycast 也能验证「WS 接入 → 路由 → 生成 → TTS → 播放」整链路。

用法：python scripts/ws_send_test.py [--interval 秒]（默认逐条间隔 2 秒）
"""
import argparse
import asyncio
import json
import time

import websockets

URI = "ws://127.0.0.1:8765"

EVENTS = [
    {"timestamp": 0, "method": "WebcastChatMessage",
     "user": {"name": "测试观众A"}, "content": "主播这是什么游戏"},
    {"timestamp": 0, "method": "WebcastChatMessage",
     "user": {"name": "测试观众B"}, "content": "主播这是什么游戏呀"},
    {"timestamp": 0, "method": "WebcastGiftMessage",
     "user": {"name": "测试观众A"},
     "gift": {"name": "小心意", "count": 2, "price": 100, "repeatEnd": True}},
    {"timestamp": 0, "method": "WebcastSocialMessage",
     "user": {"name": "测试观众B"}},
]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=2.0,
                        help="逐条发送间隔秒数（默认 2）")
    args = parser.parse_args()

    print(f"连接 {URI} ……")
    async with websockets.connect(URI) as ws:
        print("[已连接] 开始发送 4 条模拟事件（弹幕×2、礼物×1、关注×1）")
        for ev in EVENTS:
            ev = dict(ev)
            ev["timestamp"] = int(time.time() * 1000)
            await ws.send(json.dumps(ev, ensure_ascii=False))
            label = ev["method"].replace("Webcast", "").replace("Message", "")
            print(f"  已发送: {label} {ev.get('user', {}).get('name', '')}"
                  f" {ev.get('content', ev.get('gift', {}).get('name', ''))}")
            await asyncio.sleep(args.interval)
        print("[完成] 事件已全部送达。观察工具控制台的 [事件]/[播放] 输出，"
              "以及直播伴侣音轨波动即可确认整链路。")
    return 0


if __name__ == "__main__":
    sys_exit = asyncio.run(main())
    raise SystemExit(sys_exit)
