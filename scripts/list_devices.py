"""列出系统音频设备（步骤 4.5 配套工具）。

用途：安装 VB-Cable 并重启后，运行本脚本确认虚拟声卡就绪——
「播放」侧出现 CABLE Input（输出目标，config audio.device 填这个名字）、
「录制」侧出现 CABLE Output（直播伴侣麦克风选这个名字）。
无需启动中枢即可独立检查；Player 启动自检的设备校验是第二道保险。

用法：python scripts/list_devices.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    try:
        import sounddevice as sd
    except Exception as e:
        print(f"[错误] sounddevice 初始化失败：{e}")
        return 1

    devices = list(sd.query_devices())
    cable_out = []   # 录制侧 CABLE Output（伴侣麦克风）
    cable_in = []    # 播放侧 CABLE Input（中枢输出）
    print("── 系统音频设备 ──")
    for i, d in enumerate(devices):
        name = d["name"]
        io = f"in:{d['max_input_channels']} out:{d['max_output_channels']}"
        print(f"  {i:2d}: {name} | {io} | {int(d['default_samplerate'])}Hz")
        if "CABLE" in name:
            if d["max_output_channels"] > 0:
                cable_in.append(name)
            if d["max_input_channels"] > 0:
                cable_out.append(name)

    print("──────────────────")
    if cable_in and cable_out:
        print(f"[就绪] VB-Cable 已安装：")
        print(f"  · 中枢输出（config.yaml 的 audio.device）：{cable_in[0]}")
        print(f"  · 直播伴侣麦克风（录制侧）：{cable_out[0]}")
        print("  下一步：把 config.yaml 的 audio.device 改为上面的播放设备名，")
        print("  重放验证后按 RUNBOOK 第 1 节开播。")
        return 0
    print("[未装] 未检测到 VB-Cable（无 CABLE 设备）。")
    print("  从 vb-audio.com 下载安装并重启电脑后重跑本脚本。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
