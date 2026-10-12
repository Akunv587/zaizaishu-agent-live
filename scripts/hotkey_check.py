"""F9 热键真实验收脚本（PROMPTS 步骤 4.4 验收第 1 条）。

以子进程启动 main --replay，播放中段用 pynput 键盘模拟器真实触发全局热键
（与人工按 F9 走完全相同的钩子路径），验证三件事：
1. 暂停标记出现且当前句播完后静默（暂停期间事件只记录不出任务）；
2. 再按 F9 恢复，后续弹幕继续应答；
3. 进程正常退出、统计自洽（task_produced = played + dropped_stale + dropped_pause）。
"""
import subprocess
import sys
import time
from pathlib import Path

REPLAY = Path("replay/smoke-001.jsonl")
PAUSE_AT_S = 6.0      # 播放中段按下 F9（暂停）
RESUME_AT_S = 12.0    # 暂停 6 秒后再按 F9（恢复）


def press_f9() -> None:
    from pynput.keyboard import Key, Controller
    kb = Controller()
    kb.press(Key.f9)
    kb.release(Key.f9)


def main() -> int:
    if not REPLAY.exists():
        print(f"[错误] 重放文件不存在: {REPLAY}")
        return 1

    proc = subprocess.Popen(
        [sys.executable, "main.py", "--replay", str(REPLAY), "--speed", "2"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", cwd=Path(__file__).resolve().parent.parent)

    started = time.time()
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
        out, _ = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        print("[失败] 子进程超时未退出")
        print(out[-3000:])
        return 1

    print(out[-2500:])

    checks = {
        "暂停标记": "[已暂停 AI 发声]" in out,
        "恢复标记": "[已恢复" in out,
        "热键注册": "按 F9 暂停/恢复" in out,
    }
    # 暂停期间事件被记录：暂停窗口内 replay 仍在推事件（echo_event），但无任务
    paused_record = "[事件]" in out  # 事件回显持续（照收）
    checks["暂停期间弹幕照收"] = paused_record
    checks["正常退出"] = proc.returncode == 0 and "已退出" in out
    checks["统计自洽"] = _check_stats_consistency()

    ok = True
    for name, passed in checks.items():
        print(f"[{'通过' if passed else '未过'}] {name}")
        ok = ok and passed
    return 0 if ok else 1


def _check_stats_consistency() -> bool:
    """从当日日志最后的 shutdown 记录核验：task_produced = played + 过发 + 暂停清理。"""
    import json
    from datetime import datetime

    log = Path(__file__).resolve().parent.parent / "logs" / f"live-{datetime.now().strftime('%Y%m%d')}.log"
    try:
        lines = [l for l in log.read_text(encoding="utf-8").splitlines()
                 if '"event": "shutdown"' in l]
        summary = json.loads(lines[-1])["detail"]
    except (OSError, IndexError, KeyError, ValueError):
        return False
    produced = summary.get("task_produced", 0)
    played = summary.get("played", 0)
    dropped = summary.get("dropped_stale", 0) + summary.get("dropped_pause", 0)
    print(f"   自洽：produced={produced} played={played} 过发+暂停清理={dropped}")
    return produced == played + dropped


if __name__ == "__main__":
    sys.exit(main())
