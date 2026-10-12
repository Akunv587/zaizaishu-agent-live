"""定时口播真实验收脚本（PROMPTS 步骤 5.1 验收第 2 条）。

以子进程重放稀疏长样本（replay/quiet-001.jsonl，事件后长静默），
用临时配置把 interval_min 压到 1 分钟（校验允许的下限），
在 speed=5 下整场约 2 分钟墙钟内应出现至少一次口播入队且被播放。
临时配置写在 logs/（不入 git），不改用户 config.yaml。

用法：python scripts/schedule_check.py
"""
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REPLAY = ROOT / "replay" / "quiet-001.jsonl"
TMP_CONFIG = ROOT / "logs" / "schedule-check-config.yaml"
SPEED = 5


def build_tmp_config() -> Path:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg.setdefault("schedule", {})["interval_min"] = 1
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


def main() -> int:
    if not REPLAY.exists():
        print(f"[错误] 重放文件不存在: {REPLAY}")
        return 1
    config_path = build_tmp_config()
    print(f"临时配置：interval_min=1 分钟（{config_path}）")

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
        return 1

    print(out[-1200:])
    checks = {
        "口播任务入队": "src=schedule" in out,
        "正常退出": proc.returncode == 0 and "已退出" in out,
    }
    # 口播不出现在互动密集时段：每次入队前 10 秒内不应有弹幕事件
    log = ROOT / "logs" / f"live-{__import__('datetime').datetime.now().strftime('%Y%m%d')}.log"
    quiet = None
    try:
        import datetime as _dt

        def ts_of(line: str):
            raw = line.split('"ts": "')[1]
            return _dt.datetime.fromisoformat(raw.split('"')[0])

        lines = log.read_text(encoding="utf-8").splitlines()
        echoes = [ts_of(l) for l in lines if '"event": "echo_event"' in l]
        announced = [ts_of(l) for l in lines if '"event": "idle_task_enqueued"' in l]
        if announced:
            quiet = all(
                all((t - e).total_seconds() >= 10 for e in echoes if e <= t)
                for t in announced)
        checks["口播落在静默时段"] = bool(quiet) and bool(announced)
    except (OSError, ValueError):
        checks["口播落在静默时段"] = False
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
    print(f"   自洽：produced={produced} played={played} 过发+暂停清理={dropped}")
    return produced == played + dropped


if __name__ == "__main__":
    sys.exit(main())
