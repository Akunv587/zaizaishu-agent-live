"""VB-Cable 通路测试（步骤 4.5 验收配套）。

验证：声音写入「CABLE Input（播放侧）」后能从「CABLE Output（录制侧）」录到。
做法：录制流从 CABLE Output 收音，同时用 RawOutputStream（与 Player 同款
打开方式）向 CABLE Input 写 2 秒 440Hz 测试音，再检查录音 RMS/峰值——非静音
即通路正常（直播伴侣从同一录制侧采集，故此测试等价于"直播间能听到 AI
声音"的设备层前提）。

用法：python scripts/cable_check.py
"""
import array
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sounddevice as sd  # noqa: E402

PLAY_DEVICE = "CABLE Input (VB-Audio Virtual C"     # MME 播放侧（实测唯一可用组合）
REC_DEVICE = "CABLE Output (VB-Audio Virtual "      # MME 录制侧
SR = 24000
CHUNK_S = 0.1          # 每块 100ms，模拟流式写入节奏


def find_device(name: str, want_output: bool) -> int:
    for i, d in enumerate(sd.query_devices()):
        if d["name"] == name and (
                (want_output and d["max_output_channels"] > 0)
                or (not want_output and d["max_input_channels"] > 0)):
            return i
    raise SystemExit(f"[错误] 找不到设备：{name}")


def main() -> int:
    play_idx = find_device(PLAY_DEVICE, want_output=True)
    rec_idx = find_device(REC_DEVICE, want_output=False)
    print(f"播放侧：[{play_idx}] {PLAY_DEVICE}")
    print(f"录制侧：[{rec_idx}] {REC_DEVICE}")

    total_s = 2.0
    n = int(SR * total_s)
    tone = array.array("h", (int(8000 * math.sin(2 * math.pi * 440 * i / SR))
                             for i in range(n)))
    chunks = [tone[i:i + int(SR * CHUNK_S)] for i in range(0, n, int(SR * CHUNK_S))]

    recorded = []   # 回调实时积累，避免阻塞式 read 溢出丢数据

    def on_audio(indata, frames, time_info, status):
        recorded.append(bytes(indata))

    rec = sd.RawInputStream(device=rec_idx, samplerate=SR, channels=1,
                            dtype="int16", callback=on_audio)
    rec.start()
    print(f"录制中……用 RawOutputStream 向 CABLE Input 流式写 {total_s:.0f} 秒 440Hz 测试音")

    out = sd.RawOutputStream(samplerate=SR, channels=1, dtype="int16", device=play_idx)
    out.start()
    for chunk in chunks:
        out.write(chunk.tobytes())
        time.sleep(CHUNK_S * 0.6)   # 流式节奏，给录制侧留时间
    time.sleep(0.5)                  # 尾部余量
    out.stop(); out.close()
    time.sleep(0.2)
    rec.stop(); rec.close()
    data = b"".join(recorded)

    samples = array.array("h")
    samples.frombytes(data)
    peak = max((abs(s) for s in samples), default=0)
    rms = math.sqrt(sum(s * s for s in samples) / max(len(samples), 1))
    print(f"录音结果：峰值 {peak}（int16 满量程 32767）  RMS {rms:.0f}")

    if peak > 500:
        print("[通过] 虚拟声卡通路正常：CABLE Input → CABLE Output 有声音流过。")
        print("       直播伴侣麦克风选择同名 CABLE Output 即可采集到 AI 声音。")
        return 0
    print("[未过] 录到的接近静音：检查 Windows 声音设置中 CABLE Input 是否被禁用/音量为 0。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
