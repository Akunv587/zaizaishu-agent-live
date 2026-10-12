"""看门狗真实验收脚本（PROMPTS 步骤 5.2 验收第 2 条）。

两段式验收：
1. 子进程整场重放（replay/quiet-001.jsonl，事件集中在开头后长静默）：
   临时配置把 silent_minutes 压到 1 分钟（校验允许的下限），speed=5 下
   事件位于墙钟 0/6/12s、静默至 120s——30 秒巡检在第 90s 一轮发现静默
   超过 60s，应发桌面通知（plyer 真实弹出，控制台同步打印）并投递
   兜底口播被播出，整场统计自洽。
2. 进程内直驱失败计数告警：真实时钟与 plyer，llm_fail 连续累计到阈值
   触发汇总通知并复位计数（重放链路 LLM 正常时不产生失败，此路径只能
   直驱验证）。

临时配置写在 logs/（不入 git），不改用户 config.yaml。
用法：python scripts/watchdog_check.py
"""
import asyncio
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 脚本目录启动时 hub 包不在 sys.path

REPLAY = ROOT / "replay" / "quiet-001.jsonl"
TMP_CONFIG = ROOT / "logs" / "watchdog-check-config.yaml"
SPEED = 5


def build_tmp_config() -> Path:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg.setdefault("watchdog", {})["silent_minutes"] = 1
    # 词库按配置文件所在目录解析相对路径，临时配置放 logs/ 故写绝对路径
    safety = cfg.setdefault("safety", {})
    wf = str(safety.get("block_words_file", "") or "").strip()
    wf_path = Path(wf)
    if not wf_path.is_absolute():
        wf_path = ROOT / wf_path
    safety["block_words_file"] = str(wf_path)
    TMP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    TMP_CONFIG.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return TMP_CONFIG


def check_replay_run() -> bool:
    """第 1 段：子进程整场重放，看门狗在真实 30s 巡检节奏下触发静默告警。"""
    if not REPLAY.exists():
        print(f"[错误] 重放文件不存在: {REPLAY}")
        return False
    config_path = build_tmp_config()
    print(f"临时配置：silent_minutes=1 分钟（{config_path}），子进程重放约 2 分钟，请留意桌面通知")

    proc = subprocess.Popen(
        [sys.executable, "main.py", "--replay", str(REPLAY),
         "--speed", str(SPEED), "--config", str(config_path)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", cwd=ROOT)
    try:
        out, _ = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        print("[失败] 子进程超时未退出")
        print(out[-2000:])
        return False

    print(out[-1200:])
    checks = {
        "静默告警触发": "看门狗" in out and "弹幕静默告警" in out,
        "兜底口播播出": "src=watchdog" in out,
        "正常退出": proc.returncode == 0 and "已退出" in out,
    }
    checks["统计自洽"] = _check_stats_consistency()
    ok = True
    for name, passed in checks.items():
        print(f"[{'通过' if passed else '未过'}] {name}")
        ok = ok and passed
    return ok


async def drive_fail_alert() -> bool:
    """第 2 段：进程内直驱——真实时钟/plyer，失败计数达阈值触发汇总通知并复位。"""
    from hub.queue import PrioritySpeakQueue
    from hub.stats import StatsCollector
    from hub.watchdog import Watchdog, default_notify

    print("\n── 失败计数告警直驱（真实 plyer 通知） ──")
    stats = StatsCollector()
    queue = PrioritySpeakQueue(config={})
    stop = asyncio.Event()
    notes = []

    def notify(title, message):
        notes.append((title, message))
        print(f"[通知] {title}: {message}")
        default_notify(title, message)  # 真实桌面通知
        stop.set()

    watchdog = Watchdog(queue, stats=stats,
                        config={"watchdog": {"silent_minutes": 999,
                                             "fail_threshold": 3}},
                        notify=notify, interval_s=0.5)
    stats.inc("llm_fail", 2)  # 未达阈值，首轮巡检不应报
    await asyncio.sleep(0.6)  # 一轮空巡检
    stats.inc("llm_fail", 1)  # 达到 3 次
    await watchdog.run(stop)  # 下一轮巡检触发告警并复位

    checks = {
        "告警通知发出": len(notes) == 1,
        "计数已复位": stats.get("llm_fail") == 0,
        "静默未误报": stats.get("silent_fallback") == 0,
    }
    ok = True
    for name, passed in checks.items():
        print(f"[{'通过' if passed else '未过'}] {name}")
        ok = ok and passed
    return ok


def _check_stats_consistency() -> bool:
    """从当日日志最后的 shutdown 记录核验：task_produced = played + 过发 + 暂停清理。"""
    import json
    from datetime import datetime

    log = ROOT / "logs" / f"live-{datetime.now().strftime('%Y%m%d')}.log"
    try:
        lines = [l for l in log.read_text(encoding="utf-8").splitlines()
                 if '"event": "shutdown"' in l]
        summary = json.loads(lines[-1])["detail"]
    except (OSError, IndexError, KeyError, ValueError):
        return False
    produced = summary.get("task_produced", 0)
    played = summary.get("played", 0)
    dropped = summary.get("dropped_stale", 0) + summary.get("dropped_pause", 0)
    silent_fallback = summary.get("silent_fallback", 0)
    print(f"   自洽：produced={produced} played={played} 过发+暂停清理={dropped}"
          f" 兜底口播={silent_fallback}")
    return produced == played + dropped and silent_fallback >= 1


def main() -> int:
    ok_replay = check_replay_run()
    ok_fail = asyncio.run(drive_fail_alert())
    print("\n验收结论：", "通过" if ok_replay and ok_fail else "未通过")
    return 0 if ok_replay and ok_fail else 1


if __name__ == "__main__":
    sys.exit(main())
