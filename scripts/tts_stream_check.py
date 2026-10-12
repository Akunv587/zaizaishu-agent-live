"""步骤 4.2 验收手测：真实密钥流式合成 + 向导非流式路径回归。

用法（zaizaishu-agent-live 目录下）：
    python scripts\\tts_stream_check.py

验收点（PROMPTS 步骤 4.2）：
  1. hub 的 synthesize_stream 用 demoVoice001 合成约 30 字话术，
     首包到达时间 < 1 秒，PCM 落盘 logs\\tts_stream_check.wav 可试听；
  2. 非流式 synthesize()（向导「合成并试听」走的路径）仍正常出声，
     落盘 logs\\tts_stream_check.mp3。
"""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hub.config import load_config  # noqa: E402
from hub.tts import TTSClient, save_wav  # noqa: E402

TEXT = "兄弟们这款游戏是真的好玩，点点小手柄就能下载，不会的看评论区置顶哈"


async def stream_check(tts: TTSClient) -> None:
    print("── 流式合成（hub 的 synthesize_stream） ──")
    t0 = time.perf_counter()
    first = None
    pcm = bytearray()
    async for chunk in tts.synthesize_stream(TEXT):
        if first is None:
            first = time.perf_counter() - t0
            print(f"首包到达: {first:.3f}s  {'✓ < 1s' if first < 1 else '✗ 超标'}")
        pcm += chunk
    elapsed = time.perf_counter() - t0
    audio_s = len(pcm) / 2 / 24000
    print(f"合成完成: {elapsed:.2f}s  共 {len(pcm)} 字节 PCM（约 {audio_s:.1f} 秒音频，"
          f"{len(pcm) * 1000 // max(int(audio_s * 1000), 1) / 1000:.0f}KB/s）")
    out = ROOT / "logs" / "tts_stream_check.wav"
    save_wav(bytes(pcm), out)
    print(f"已落盘: {out}（请本地播放试听克隆音色）")


def nonstream_check(tts: TTSClient) -> None:
    print("── 非流式回归（向导「合成并试听」同路径） ──")
    client = tts._client  # MiniMaxVoiceClient
    res = client.synthesize(TEXT, voice_id=tts._voice_id, format_="hex",
                            audio_format="mp3")
    out = ROOT / "logs" / "tts_stream_check.mp3"
    client.save_audio(res, str(out))
    print(f"已落盘: {out}（向导路径回归正常）")


def main() -> int:
    cfg = load_config(str(ROOT / "config.yaml"))
    tts = TTSClient(config=cfg)
    print(f"音色: {tts._voice_id}  端点: {tts._client.endpoint}  "
          f"模型: {tts._client.model}\n话术: 「{TEXT}」（{len(TEXT)} 字）")
    asyncio.run(stream_check(tts))
    nonstream_check(tts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
