"""webui_smoke.py —— 控制台冒烟验证：连 /ws 收快照，注入测试弹幕/礼物，确认事件回流。

音色工坊断言（确定性路径，不真实调用 MiniMax、不改任何配置）：
vc_get 状态、二进制分片聚合上传、上传中止、复刻/试听参数前置校验。
"""
import asyncio
import json
import sys

from websockets.asyncio.client import connect

URL = "ws://127.0.0.1:8766/ws"


async def recv_type(ws, want: str, timeout: float = 5.0) -> dict:
    """收取下一条指定类型的消息（跳过期间其它广播）。"""
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if msg.get("type") == want:
            return msg


async def vc_checks(ws) -> bool:
    """音色工坊协议冒烟：全部走前置校验/确定性报错，不发真实网络请求。"""
    results = []

    def check(label: str, cond: bool) -> None:
        results.append(cond)
        print(f"[vc] {label}: {'通过' if cond else '失败'}")

    # 1) 状态查询：MiniMax 配置 + 音色库
    await ws.send(json.dumps({"action": "vc_get"}))
    status = (await recv_type(ws, "vc"))["data"]
    mini, voices = status.get("minimax", {}), status.get("voices")
    print(f"[vc] status configured={mini.get('configured')} "
          f"endpoint={mini.get('endpoint')} 音色库={len(voices) if isinstance(voices, list) else '?'}条")
    check("vc_get 状态", status.get("op") == "status"
          and isinstance(mini.get("configured"), bool)
          and isinstance(voices, list))

    # 2) 分片聚合上传：小于 2048 字节 → 体检报错（先于任何 MiniMax 调用）
    await ws.send(json.dumps({"action": "vc_upload", "name": "冒烟测试.wav"}))
    raw = b"RIFF" + b"\x00" * 992 + b"WAVE"  # 1000 字节，魔数对但小于下限
    for i in range(0, len(raw), 400):
        await ws.send(raw[i:i + 400])
    await ws.send(json.dumps({"action": "vc_upload_end"}))
    err = (await recv_type(ws, "vc"))["data"]
    print(f"[vc] 过小音频 -> {err.get('error', '')[:44]}")
    check("上传分片聚合+体检报错", err.get("op") == "error"
          and err.get("stage") == "upload" and "太小" in err.get("error", ""))

    # 3) 上传中止：abort 后 end → 会话不存在
    await ws.send(json.dumps({"action": "vc_upload", "name": "中止测试.wav"}))
    await ws.send(b"RIFF" + b"\x00" * 64)
    await ws.send(json.dumps({"action": "vc_upload_abort"}))
    await ws.send(json.dumps({"action": "vc_upload_end"}))
    err = (await recv_type(ws, "vc"))["data"]
    check("上传中止", err.get("op") == "error" and err.get("stage") == "upload"
          and "上传会话不存在" in err.get("error", ""))

    # 4) 复刻参数校验：非法 voice_id → 前置报错（不外呼）
    await ws.send(json.dumps({"action": "vc_clone", "file_id": 123,
                              "voice_id": "ab", "voice_name": "冒烟"}))
    err = (await recv_type(ws, "vc"))["data"]
    check("复刻 voice_id 校验", err.get("op") == "error"
          and err.get("stage") == "clone" and "8~256" in err.get("error", ""))

    # 5) 试听参数校验：空文本 → 前置报错（不外呼）
    await ws.send(json.dumps({"action": "vc_preview", "voice_id": "", "text": ""}))
    err = (await recv_type(ws, "vc"))["data"]
    check("试听空文本校验", err.get("op") == "error"
          and err.get("stage") == "preview" and "文字" in err.get("error", ""))

    return all(results)


async def main() -> int:
    got_snapshot = got_feed = vc_ok = False
    seen_events: list[dict] = []
    async with connect(URL) as ws:
        for _ in range(2):  # snapshot + feed
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if msg["type"] == "snapshot":
                got_snapshot = True
                print(f"[snapshot] paused={msg['paused']} source={msg['source']} "
                      f"queue={msg['queue']} voice={msg['config']['voice_id']}")
            elif msg["type"] == "feed":
                got_feed = True
                print(f"[feed] 历史条数={len(msg['data'])}")

        for kind in ("chat", "gift"):
            await ws.send(json.dumps({"action": "test", "kind": kind}))
            print(f"[sent] 测试事件 {kind}")

        deadline = asyncio.get_event_loop().time() + 8
        while asyncio.get_event_loop().time() < deadline and len(seen_events) < 2:
            try:
                msg = json.loads(await asyncio.wait_for(
                    ws.recv(), timeout=max(0.1, deadline - asyncio.get_event_loop().time())))
            except asyncio.TimeoutError:
                break
            if msg["type"] == "event":
                seen_events.append(msg["data"])
                d = msg["data"]
                print(f"[event] {d['kind']} type={d.get('type', '')} "
                      f"user={d.get('user', '')} {d.get('content', d.get('text', ''))!r}")
        await ws.send(json.dumps({"action": "toggle_pause"}))
        await asyncio.sleep(0.5)
        await ws.send(json.dumps({"action": "toggle_pause"}))
        await asyncio.sleep(0.5)

        vc_ok = await vc_checks(ws)

    print(f"\n结果: snapshot={got_snapshot} feed={got_feed} "
          f"事件回流={len(seen_events)}/2 暂停往返=已发 音色工坊={'通过' if vc_ok else '失败'}")
    ok = got_snapshot and got_feed and len(seen_events) >= 2 and vc_ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
