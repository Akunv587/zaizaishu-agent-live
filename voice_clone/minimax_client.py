# -*- coding: utf-8 -*-
"""
MiniMax 声音克隆客户端库（零第三方依赖，仅用 Python 标准库）
===============================================================
实现「上传音频 → 复刻音色 → 合成语音」全流程，可直接 import 到你的下游工具中使用。

用法：
    from minimax_client import MiniMaxVoiceClient
    client = MiniMaxVoiceClient(api_key="你的API Key")

    # 1) 上传待克隆音频，拿 file_id
    fid = client.upload_audio("my_voice.mp3", purpose="voice_clone")

    # 2) 复刻音色，拿 voice_id 和试听
    res = client.clone_voice(file_id=fid, voice_id="MyVoiceId01", model="speech-2.8-hd")
    print(res["demo_audio"])       # 试听音频 URL

    # 3) 用克隆音色合成语音（output_format="url" 直接拿下载链接，="hex" 拿二进制）
    res = client.synthesize("你好，这是我的克隆音色。", voice_id="MyVoiceId01")
    client.save_audio(res, "out.mp3")

    # 3b) 流式合成（直播链路用）：逐块 yield PCM bytes（24000Hz int16 单声道）
    for pcm_chunk in client.synthesize_stream("你好兄弟们", voice_id="MyVoiceId01"):
        ...  # 首包即写声卡

命令行快速体验（省去写脚本）：
    python minimax_client.py --api-key KEY upload my_voice.mp3
    python minimax_client.py --api-key KEY clone <file_id> MyVoiceId01
    python minimax_client.py --api-key KEY tts "你好" MyVoiceId01 --save out.mp3

注意：
  * 默认使用国内端点 https://api.minimaxi.com（是 MiniMax 大陆账户）。
  * 若你是海外账户，请传 endpoint="https://api.minimax.io"。
  * 复刻出的音色若 7 天内未被正式合成调用，系统会自动删除。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
import urllib.request
import urllib.error
import urllib.parse
import mimetypes
import re
import time

logger = logging.getLogger("minimax_client")

# ---------- 默认配置（可按需覆盖） ----------
DEFAULT_ENDPOINT = "https://api.minimaxi.com"     # 国内
DEFAULT_MODEL = "speech-2.8-hd"

# 官方停顿标记为 <#秒数#>（0.01~99.99s）；话术里写「|」更顺手，合成前自动转换：
# 连续 n 个竖线合并为一段停顿（|| 即 0.8s），文本已含 <# 原生标记时原样放行不插手
PAUSE_PER_PIPE_S = 0.4


def _expand_pause_marks(text: str) -> str:
    if "<#" in text:
        return text
    return re.sub(r"\|+", lambda m: f"<#{PAUSE_PER_PIPE_S * len(m.group()):.1f}#>", text)


DEFAULT_PREVIEW_TEXT = "用音色复刻技术生成的语音，音色与原声高度一致，声音自然清晰。"
# PyInstaller 冻结后 __file__ 在 _MEIPASS 临时解包目录（每次运行都变，写进去的数据会丢），
# 密钥/音色库必须锚定 exe 旁的 voice_clone/voice_clone_data（与 hub.paths.EXE_DIR 一致）；
# 源码运行时本文件就在 voice_clone/ 内，数据目录是同级 voice_clone_data
if getattr(sys, "frozen", False):
    CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                              "voice_clone", "voice_clone_data")
else:
    CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "voice_clone_data")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


class MiniMaxError(Exception):
    """MiniMax API 返回的业务错误。"""

    def __init__(self, status_code, status_msg, trace_id=None, extra=None):
        self.status_code = status_code
        self.status_msg = status_msg
        self.trace_id = trace_id
        self.extra = extra or {}
        super().__init__(f"{status_msg} (status_code={status_code}) trace_id={trace_id}")


def _headers(api_key, content_type=None):
    h = {"Authorization": f"Bearer {api_key}"}
    if content_type:
        h["Content-Type"] = content_type
    return h


def _multipart(fields: dict, files: dict):
    """把普通字段和文件封装成 multipart/form-data 的 (body_bytes, content_type)。"""
    boundary = "----minimax" + uuid.uuid4().hex
    chunks = []
    for k, v in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8")
        )
    for name, (filename, content, content_type) in files.items():
        chunks.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"'
             f'\r\nContent-Type: {content_type}\r\n\r\n').encode("utf-8")
        )
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _request_json(url, api_key, method="GET", body=None, content_type=None, timeout=120):
    """发送请求并解析 JSON 响应，统一校验 base_resp。"""
    data = None
    h = _headers(api_key, content_type)
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode("utf-8")
    elif isinstance(body, bytes):
        data = body
    elif isinstance(body, str):
        data = body.encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        raise MiniMaxError(getattr(e, "code", -1), f"HTTP 错误：{e.code}", extra={"http_body": raw[:2000]})
    except urllib.error.URLError as e:
        raise MiniMaxError(-1, f"网络请求失败：{e.reason}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise MiniMaxError(-1, "返回内容不是有效 JSON", extra={"body": raw[:2000]})

    base = data.get("base_resp") or {}
    if base.get("status_code") not in (0, None):
        raise MiniMaxError(
            base.get("status_code", -1),
            base.get("status_msg", "未知错误"),
            data.get("trace_id"),
            data,
        )
    return data


class MiniMaxVoiceClient:
    def __init__(self, api_key: str, endpoint: str = DEFAULT_ENDPOINT,
                 model: str = DEFAULT_MODEL, timeout: int = 120):
        self.api_key = api_key.strip()
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout

    # ---------------- 1. 上传音频 ----------------
    def upload_audio(self, file_path: str, purpose: str = "voice_clone") -> str:
        """
        上传音频文件，返回 file_id。
        purpose: "voice_clone"（待克隆音频 10s~5min）或 "prompt_audio"（参考音频 <8s）。
        支持 mp3 / m4a / wav。
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(file_path)
        content = open(file_path, "rb").read()
        fname = os.path.basename(file_path)
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        body, ct = _multipart({"purpose": purpose}, {"file": (fname, content, ctype)})
        url = f"{self.endpoint}/v1/files/upload"
        resp = _request_json(url, self.api_key, method="POST", body=body,
                             content_type=ct, timeout=self.timeout)
        return resp["file"]["file_id"]

    # ---------------- 2. 复刻音色 ----------------
    def clone_voice(self, file_id, voice_id: str, model: str = None,
                    prompt_audio=None, prompt_text=None,
                    text: str = DEFAULT_PREVIEW_TEXT,
                    need_noise_reduction: bool = False,
                    need_volume_normalization: bool = False,
                    language_boost: str = None) -> dict:
        """
        复刻音色，返回含 demo_audio(试听链接) 等信息的字典。
        prompt_audio / prompt_text 需成对出现（增强相似度与稳定性，可选）。
        """
        payload = {
            "file_id": file_id,
            "voice_id": voice_id,
            "text": text,
            "model": model or self.model,
            "need_noise_reduction": need_noise_reduction,
            "need_volume_normalization": need_volume_normalization,
        }
        if prompt_audio is not None:
            if not prompt_text:
                raise ValueError("提供 prompt_audio 时必须同时提供 prompt_text")
            payload["clone_prompt"] = {"prompt_audio": prompt_audio, "prompt_text": prompt_text}
        if language_boost:
            payload["language_boost"] = language_boost

        url = f"{self.endpoint}/v1/voice_clone"
        return _request_json(url, self.api_key, method="POST", body=payload,
                             content_type="application/json", timeout=self.timeout)

    # ---------------- 2b. 查询账号可用音色 ----------------
    def get_voice(self, voice_type: str = "all") -> dict:
        """查询当前 API Key 账号下可调用的全部音色（官方「查询可用音色ID」接口）。

        voice_type: system / voice_cloning / voice_generation / all。
        返回原始响应字典（system_voice / voice_cloning / voice_generation 三个列表）。
        注意：快速复刻与文生音色为未激活状态，需正式合成调用一次后才会出现在列表里。
        """
        return _request_json(f"{self.endpoint}/v1/get_voice", self.api_key,
                             method="POST", body={"voice_type": voice_type},
                             content_type="application/json", timeout=self.timeout)

    # ---------------- 2c. 删除账号音色 ----------------
    def delete_voice(self, voice_id: str, voice_type: str = "voice_cloning") -> dict:
        """删除账号下的指定音色（官方「删除声音」接口，删除后不可恢复）。

        voice_type 仅支持 voice_cloning（克隆音色）/ voice_generation（文生音色），
        系统预置音色不可删除。返回原始响应字典。
        """
        if voice_type not in ("voice_cloning", "voice_generation"):
            raise ValueError("voice_type 仅支持 voice_cloning / voice_generation")
        return _request_json(f"{self.endpoint}/v1/delete_voice", self.api_key,
                             method="POST",
                             body={"voice_type": voice_type, "voice_id": voice_id},
                             content_type="application/json", timeout=self.timeout)

    # ---------------- 3. 用克隆音色合成语音 ----------------
    def synthesize(self, text: str, voice_id: str, model: str = None,
                   format_: str = "url",            # "url" 直接返回可下载链接 / "hex" 返回二进制
                   output_file: str = None,         # 若设置则将音频保存到该路径
                   sample_rate: int = 32000, bitrate: int = 128000,
                   audio_format: str = "mp3", speed: int | float = 1,
                   vol: int | float = 1, pitch: int | float = 0,
                   emotion: str = None) -> dict:
        """
        使用克隆音色合成语音，返回原始响应字典。
        设置 output_file 时会自动把音频写到本地文件，并返回 {"path": ...}。
        speed 0.5~2.0 / vol 0~10 接受浮点，pitch -12~+12 取整（半音）。
        文本中的「|」自动转换为官方停顿标记 <#秒数#>（每竖线 0.4s）。
        emotion: 可选，控制语气情绪，speech-2.8-hd 支持
          happy/sad/angry/fearful/disgusted/surprised/calm/fluent
          例：emotion="happy" 让克隆音色用高兴的语气说话。
        """
        payload = {
            "model": model or self.model,
            "text": _expand_pause_marks(text),
            "stream": False,
            "output_format": format_,
            "voice_setting": {
                "voice_id": voice_id,
                "speed": round(float(speed), 2), "vol": round(float(vol), 1),
                "pitch": int(round(float(pitch))),
            },
            "audio_setting": {"sample_rate": sample_rate, "bitrate": bitrate,
                              "format": audio_format, "channel": 1},
        }
        if emotion:
            payload["voice_setting"]["emotion"] = emotion
        url = f"{self.endpoint}/v1/t2a_v2"
        resp = _request_json(url, self.api_key, method="POST", body=payload,
                             content_type="application/json", timeout=self.timeout)
        if output_file:
            self.save_audio(resp, output_file, audio_format)
            resp = dict(resp)
            resp["saved_to"] = os.path.abspath(output_file)
        return resp

    # ---------------- 3b. 流式合成（直播链路用） ----------------
    def synthesize_stream(self, text: str, voice_id: str, model: str = None,
                          sample_rate: int = 24000, bitrate: int = 128000,
                          speed: int | float = 1, vol: int | float = 1,
                          pitch: int | float = 0, emotion: str = None):
        """
        stream=true 的 t2a_v2：流式读响应，逐行解析，yield PCM bytes。

        与 synthesize() 同构，差异四处：stream=true、output_format="hex"、
        audio_setting.format="pcm"、默认 sample_rate=24000。
        响应为 SSE 行序列（每行 data:{...}），data.audio 为 hex PCM 分片；
        base_resp.status_code 非 0 抛 MiniMaxError；单行解析失败跳过记日志
        不中断整条流。保持零第三方依赖（urllib 流式读取）。

        去重规则（2026-09-14 真实直播「说两遍」根修复）：status=1 为增量分块
        照常产出；status=2 收尾事件会携带「整条全量音频」（与增量分块之和逐字节
        一致），已收到增量分块时必须丢弃，否则同一正文被播放两遍；全流仅收尾
        事件带音频（极短文本无增量分块）时用它保底。
        """
        payload = {
            "model": model or self.model,
            "text": _expand_pause_marks(text),
            "stream": True,
            "output_format": "hex",
            "voice_setting": {
                "voice_id": voice_id,
                "speed": round(float(speed), 2), "vol": round(float(vol), 1),
                "pitch": int(round(float(pitch))),
            },
            "audio_setting": {"sample_rate": sample_rate, "bitrate": bitrate,
                              "format": "pcm", "channel": 1},
        }
        if emotion:
            payload["voice_setting"]["emotion"] = emotion
        url = f"{self.endpoint}/v1/t2a_v2"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers=_headers(self.api_key, "application/json"), method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            raise MiniMaxError(getattr(e, "code", -1), f"HTTP 错误：{e.code}",
                               extra={"http_body": raw[:2000]})
        except urllib.error.URLError as e:
            raise MiniMaxError(-1, f"网络请求失败：{e.reason}")

        with resp:
            buf = b""
            incremental_bytes = 0  # 已产出的增量分块（status=1）总字节数
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    audio, is_final = self._parse_sse_line(line)
                    if not audio:
                        continue
                    if is_final and incremental_bytes and len(audio) >= incremental_bytes:
                        # status=2 收尾事件携带的是整条全量音频（实测与此前增量
                        # 分块之和逐字节一致）——已播过增量再产出它会把正文播两遍。
                        # 长度护栏：若收尾音频比增量总和还短（协议变体的末块），
                        # 则不是全量重发，照常产出防截尾。
                        continue
                    if not is_final:
                        incremental_bytes += len(audio)
                    yield audio
            if buf.strip():  # 无结尾换行的最后一行
                audio, is_final = self._parse_sse_line(buf)
                if audio and not (is_final and incremental_bytes
                                  and len(audio) >= incremental_bytes):
                    yield audio

    @staticmethod
    def _parse_sse_line(line: bytes):
        """解析一行 SSE：返回 (PCM bytes 或 None, 是否 status=2 收尾事件)。

        无音频时 audio 为 None；业务错误抛 MiniMaxError。
        status=2 收尾事件的 audio 是整条全量音频（非增量），调用方负责去重。
        """
        s = line.decode("utf-8", "ignore").strip()
        if not s.startswith("data:"):
            return None, False
        body = s[5:].strip()
        if not body:
            return None, False
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("流式单行解析失败，跳过：%s", s[:120])
            return None, False
        base = data.get("base_resp") or {}
        if base.get("status_code") not in (0, None):
            raise MiniMaxError(
                base.get("status_code", -1),
                base.get("status_msg", "未知错误"),
                data.get("trace_id"),
                data,
            )
        d = data.get("data") or {}
        is_final = d.get("status") == 2
        audio = d.get("audio")
        if not audio:
            return None, is_final
        try:
            return bytes.fromhex(audio), is_final
        except ValueError:
            logger.warning("流式音频 hex 解码失败，跳过该行")
            return None, is_final

    def save_audio(self, resp: dict, output_file: str, audio_format: str = "mp3"):
        """把合成响应里的音频保存到本地文件（自动处理 url 与 hex 两种返回）。"""
        data = resp.get("data") or {}
        audio = data.get("audio")
        if not audio:
            raise MiniMaxError(-1, "响应中没有音频数据", extra=resp)
        audio_format = audio_format or (data or {}).get("audio_format", "mp3")
        if not output_file.lower().endswith("." + audio_format.lower()):
            output_file = f"{output_file}.{audio_format}"
        if audio.startswith("http://") or audio.startswith("https://"):
            with urllib.request.urlopen(audio, timeout=self.timeout) as r:
                raw = r.read()
        else:  # hex
            try:
                raw = bytes.fromhex(audio)
            except ValueError:
                raise MiniMaxError(-1, "音频字段既不是 URL 也不是合法 hex", extra=resp)
        with open(output_file, "wb") as f:
            f.write(raw)
        return output_file


# =====================================================================
#  命令行 / 配置工具（可视化向导会复用这部分）
# =====================================================================
def load_config() -> dict:
    """读取本地配置（含保存的 API Key）。"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception:
                return {}
    return {}


def save_config(cfg: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def client_from_config() -> MiniMaxVoiceClient:
    cfg = load_config()
    if not cfg.get("api_key"):
        raise MiniMaxError(-1, "尚未配置 API Key，请先运行网页向导或设置 api_key")
    return MiniMaxVoiceClient(cfg["api_key"], endpoint=cfg.get("endpoint", DEFAULT_ENDPOINT))


def _cli():
    import argparse
    p = argparse.ArgumentParser(description="MiniMax 声音克隆命令行工具")
    p.add_argument("--api-key", help="MiniMax API Key（不填则读取本地配置）")
    p.add_argument("--endpoint", default=None, help="API 端点，默认国内")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("upload", help="上传音频，输出 file_id")
    sp.add_argument("file")
    sp.add_argument("--purpose", default="voice_clone")

    sp = sub.add_parser("clone", help="复刻音色，需先上传获得 file_id")
    sp.add_argument("file_id", type=int)
    sp.add_argument("voice_id")
    sp.add_argument("--model", default=None)
    sp.add_argument("--prompt_audio_id", type=int, default=None, help="参考音频 file_id（可选）")
    sp.add_argument("--prompt_text", default=None)
    sp.add_argument("--text", default=DEFAULT_PREVIEW_TEXT)

    sp = sub.add_parser("tts", help="用克隆音色合成语音")
    sp.add_argument("text")
    sp.add_argument("voice_id")
    sp.add_argument("--model", default=None)
    sp.add_argument("--format", dest="format_", default="url", choices=["url", "hex"])
    sp.add_argument("--save", default=None)

    a = p.parse_args()
    if not a.cmd:
        p.print_help()
        sys.exit(1)

    cfg = load_config()
    api_key = a.api_key or cfg.get("api_key")
    if not api_key:
        print("错误：缺少 API Key。用 --api-key 传入，或在网页向导里配置。")
        sys.exit(2)
    endpoint = a.endpoint or cfg.get("endpoint", DEFAULT_ENDPOINT)
    c = MiniMaxVoiceClient(api_key, endpoint=endpoint)

    if a.cmd == "upload":
        fid = c.upload_audio(a.file, purpose=a.purpose)
        print(f"file_id = {fid}")
    elif a.cmd == "clone":
        res = c.clone_voice(a.file_id, a.voice_id, model=a.model,
                            prompt_audio=a.prompt_audio_id, prompt_text=a.prompt_text, text=a.text)
        print("复刻成功 voice_id =", a.voice_id)
        print("试听音频 →", res.get("demo_audio"))
    elif a.cmd == "tts":
        res = c.synthesize(a.text, a.voice_id, model=a.model, format_=a.format_, output_file=a.save)
        if a.save:
            print("已保存到 →", res["saved_to"])
        else:
            audio = res["data"]["audio"]
            print("音频(截断) →", (audio[:120] + "...") if len(audio) > 120 else audio)


if __name__ == "__main__":
    _cli()