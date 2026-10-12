"""scriptcenter_smoke.py —— v3.1 话术管理 + 系统配置页冒烟验证。

前置：应用已启动（main.py **正常模式**；重放模式不装配 cookie_configurer/room_connector，
cookie_set 会无响应挂住本脚本），控制台默认 127.0.0.1:8766（可用环境变量
ZAIZAISHU_HTTP 指定其它地址，如并行实例）。
断言（全部确定性路径，不真实调用 LLM/MiniMax）：
1) HTTP GET / 返回的四标签页含【话术管理】【系统配置】及关键元素 id
2) WS sc_list / sc_get（v2 四分区）/ sc_set_scheme 协议往返
3) Cookie 卡：cookie_get → cookie_set（登录态字段）→ 还原原值
4) HTTP GET /sc_export?pid=... 返回 zip（PK 头）
"""
import asyncio
import json
import os
import sys
import urllib.request

from websockets.asyncio.client import connect

HTTP = os.environ.get("ZAIZAISHU_HTTP", "http://127.0.0.1:8766")
WS = os.environ.get("ZAIZAISHU_WS", HTTP.replace("http://", "ws://") + "/ws")

HTML_MUST = ["tabScript", "tabConfig", "viewScript", "viewConfig",
             "cfgCookie", "btnCookieSave", "llmVendor", "selAudioDevice"]


async def recv_op(ws, want_op: str, timeout: float = 5.0, max_msgs: int = 120) -> dict:
    # 服务端每秒推 snapshot，wait_for 会被不断重置——必须再设消息总量上限，
    # 否则目标响应永不来时（如重放模式 cookie_set 无装配）会无限挂住
    for _ in range(max_msgs):
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if msg.get("type") == "script" and msg["data"].get("op") == want_op:
            return msg["data"]
    raise TimeoutError(f"recv_op: 连续收 {max_msgs} 条仍无 op={want_op}")


def check(results: list, label: str, cond: bool) -> None:
    results.append(cond)
    print(f"[sc] {label}: {'通过' if cond else '失败'}")


async def main() -> int:
    results: list = []

    # 1) 四标签页 HTML
    html = urllib.request.urlopen(HTTP + "/", timeout=5).read().decode("utf-8")
    missing = [i for i in HTML_MUST if f'id="{i}"' not in html]
    check(results, f"页面含话术管理/系统配置元素（缺 {missing or '无'}）", not missing)

    async with connect(WS) as ws:
        # 吃掉建连的 snapshot + feed
        for _ in range(2):
            await asyncio.wait_for(ws.recv(), timeout=5)

        # 2) sc_list → sc_get → sc_set_scheme 往返
        await ws.send(json.dumps({"action": "sc_list"}))
        lst = await recv_op(ws, "list")
        profiles = lst.get("profiles") or []
        active = lst.get("active") or ""
        print(f"[sc] 存档数={len(profiles)} 方案={lst.get('scheme')} 激活={active}")
        check(results, "sc_list 至少一个存档", len(profiles) >= 1)
        check(results, "sc_list 含 scheme 指针", lst.get("scheme") in ("ai", "template"))

        pid = active or (profiles[0].get("id") if profiles else "")
        await ws.send(json.dumps({"action": "sc_get", "profile_id": pid}))
        prof = (await recv_op(ws, "profile"))["profile"]
        sections = all(k in prof for k in
                       ("knowledge", "samples_ai", "samples_template",
                        "event_templates", "topics_ai", "topics_template", "schedule"))
        print(f"[sc] 存档《{prof.get('name')}》AI话题={len(prof.get('topics_ai', []))} "
              f"AI样本={len(prof.get('samples_ai', []))} "
              f"模板话题={len(prof.get('topics_template', []))} "
              f"模板样本={len(prof.get('samples_template', []))} "
              f"调度={prof.get('schedule')}")
        check(results, "sc_get v2 四分区齐全", sections)

        await ws.send(json.dumps({"action": "sc_set_scheme", "scheme": "template"}))
        back = await recv_op(ws, "scheme")
        check(results, "sc_set_scheme → template", back.get("scheme") == "template")
        await ws.send(json.dumps({"action": "sc_set_scheme", "scheme": "ai"}))
        back = await recv_op(ws, "scheme")
        check(results, "sc_set_scheme → ai（还原）", back.get("scheme") == "ai")

        # 3) Cookie 卡：查询→写入登录态串→还原
        await ws.send(json.dumps({"action": "cookie_get"}))
        c0 = await recv_op(ws, "cookie")
        print(f"[sc] cookie configured={c0.get('configured')} logged_in={c0.get('logged_in')}")
        check(results, "cookie_get 返回状态", "configured" in c0 and "logged_in" in c0)

        probe = "ttwid=smoke%3Dabc; sessionid=fake_session_for_smoke; other=1"
        await ws.send(json.dumps({"action": "cookie_set", "cookie": probe}))
        saved = await recv_op(ws, "cookie_saved", timeout=10)
        print(f"[sc] cookie_set -> {saved.get('message', '')[:50]}")
        check(results, "cookie_set 成功且识别登录态",
              saved.get("ok") is True and saved.get("logged_in") is True)

        await ws.send(json.dumps({"action": "cookie_set", "cookie": ""}))
        cleared = await recv_op(ws, "cookie_saved", timeout=10)
        check(results, "cookie 还原（清空回匿名）", cleared.get("ok") is True)

    # 4) 存档 zip 导出
    if pid:
        raw = urllib.request.urlopen(f"{HTTP}/sc_export?pid={pid}", timeout=10).read()
        print(f"[sc] 导出 zip {len(raw)} 字节")
        check(results, "sc_export 返回 zip", raw[:2] == b"PK" and len(raw) > 200)

    ok = all(results)
    print(f"\n结果: {'全部通过' if ok else '存在失败'}（{sum(results)}/{len(results)}）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
