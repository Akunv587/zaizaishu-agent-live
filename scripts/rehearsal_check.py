"""整场离线彩排验收脚本（PROMPTS 步骤 5.3）。

以子进程真实跑一场完整重放（replay/rehearsal-001.jsonl，speed=1，
约 8.4 分钟墙钟——覆盖 5 分钟周期统计与 7±1 分钟定时口播窗口），
彩排中段用 pynput 键盘模拟器真实按 F9（与人工按键走同一全局钩子路径）
验证热键与全功能同场共存。

验收口径（对照 PROMPTS 步骤 5.3）：
1. 全程无未捕获异常（Traceback）
2. 控制台可见五类事件处理、聚合合并、礼物三档结算、口播、
   热键暂停恢复轨迹，且 5 分钟周期统计出现
3. 退出时统计自洽（produced = played + 过发 + 暂停清理）
4. follow/fansclub 为概率播报（0.6/0.8），只断言事件到达轨迹（echo），
   播报与否不作为成败判据
用法：python scripts/rehearsal_check.py
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPLAY = ROOT / "replay" / "rehearsal-001.jsonl"
PAUSE_AT_S = 100.0     # 静默段按下 F9（此时段无任务，暂停语义最干净）
RESUME_AT_S = 115.0    # 15 秒后再按 F9 恢复
SPEED = 1


def press_f9() -> None:
    from pynput.keyboard import Key, Controller
    kb = Controller()
    kb.press(Key.f9)
    kb.release(Key.f9)


def main() -> int:
    if not REPLAY.exists():
        print(f"[错误] 重放文件不存在: {REPLAY}")
        return 1
    print(f"整场彩排：{REPLAY.name} speed={SPEED}，约 8.5 分钟，"
          f"t={PAUSE_AT_S:.0f}s/t={RESUME_AT_S:.0f}s 模拟 F9 暂停/恢复")

    proc = subprocess.Popen(
        [sys.executable, "main.py", "--replay", str(REPLAY), "--speed", str(SPEED)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", cwd=ROOT)

    started = time.time()
    since_iso = _now_iso()  # 只统计本次彩排窗口内写下的日志行
    try:
        while time.time() - started < PAUSE_AT_S:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        press_f9()
        print(f">> 已模拟按下 F9（暂停，t={time.time() - started:.1f}s）")
        while time.time() - started < RESUME_AT_S:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        press_f9()
        print(f">> 已模拟按下 F9（恢复，t={time.time() - started:.1f}s）")
        # 重放总时长约 505s，恢复键在 115s——剩余约 390s + 收尾，超时留足余量
        out, _ = proc.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        print("[失败] 子进程超时未退出")
        print(out[-3000:])
        return 1

    checks = {
        "无未捕获异常": "Traceback" not in out,
        "启动自检通过": "启动自检" in out and "[失败]" not in _section(out, "启动自检"),
        "五类事件到达": all(f"type={t}" in out for t in
                            ("chat", "gift", "follow", "fansclub", "enter")),
        "聚合合并轨迹": "合并2" in out,
        "礼物三档结算": ("档位low" in out) and ("档位mid" in out) and ("档位high" in out),
        "礼物感谢播出": out.count("src=gift") >= 3,
        "口播轨迹": "src=schedule" in out,
        "热键暂停标记": "[已暂停 AI 发声]" in out,
        "热键恢复标记": "[已恢复" in out,
        "周期统计出现": "periodic_stats" in _today_log(since_iso),
        "正常退出": proc.returncode == 0 and "已退出" in out,
        "统计自洽": _check_stats_consistency(since_iso),
    }
    print(out[-1500:])
    ok = True
    for name, passed in checks.items():
        print(f"[{'通过' if passed else '未过'}] {name}")
        ok = ok and passed
    print("\n彩排结论：", "通过" if ok else "未通过")
    return 0 if ok else 1


def _section(out: str, title: str) -> str:
    """截取控制台输出中「── title ─」到下一条横线之间的自检块。"""
    marker = f"── {title} "
    start = out.find(marker)
    if start < 0:
        return ""
    end = out.find("─────", start + len(marker))
    return out[start:end] if end > 0 else out[start:]


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="milliseconds")


def _today_log(since_iso: str = None) -> str:
    """当日日志文本；指定 since_iso 时只保留该时刻之后写下的行。"""
    from datetime import datetime

    log = ROOT / "logs" / f"live-{datetime.now().strftime('%Y%m%d')}.log"
    try:
        lines = log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    if since_iso is None:
        return "\n".join(lines)
    kept = []
    for line in lines:
        idx = line.find('"ts": "')
        if idx >= 0 and line[idx + 7:idx + 30] >= since_iso:
            kept.append(line)
    return "\n".join(kept)


def _check_stats_consistency(since_iso: str = None) -> bool:
    """从当日日志（彩排窗口内）最后的 shutdown 记录核验：task_produced = played + 过发 + 暂停清理。"""
    import json
    from datetime import datetime

    log = ROOT / "logs" / f"live-{datetime.now().strftime('%Y%m%d')}.log"
    try:
        lines = [l for l in log.read_text(encoding="utf-8").splitlines()
                 if '"event": "shutdown"' in l
                 and (since_iso is None or (l.find('"ts": "') >= 0
                                            and l[l.find('"ts": "') + 7:l.find('"ts": "') + 30] >= since_iso))]
        summary = json.loads(lines[-1])["detail"]
    except (OSError, IndexError, KeyError, ValueError):
        return False
    produced = summary.get("task_produced", 0)
    played = summary.get("played", 0)
    dropped = summary.get("dropped_stale", 0) + summary.get("dropped_pause", 0)
    print(f"   自洽：produced={produced} played={played} 过发+暂停清理={dropped}"
          f" 口播={summary.get('idle_announced', 0)}")
    return produced == played + dropped


if __name__ == "__main__":
    sys.exit(main())
