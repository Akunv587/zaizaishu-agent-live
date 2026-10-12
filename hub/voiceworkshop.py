"""音色工坊（用户需求：MiniMax Key 配置与音色克隆管理内置控制台）。

把原独立向导（voice_clone_server.py + 8660 端口，已退役）的四步能力
合入中枢控制台：Key 配置 → 上传音频 → 复刻音色 → 合成试听 + 音色库管理。

数据落 voice_clone/voice_clone_data/（密钥 config.json、音色库
voice_library.json、上传音频 uploads/；冻结模式经 minimax_client.CONFIG_DIR
锚定 exe 旁，与 hub.paths.EXE_DIR 同一约定）。

MiniMax 调用全是同步 urllib——webui 侧一律 asyncio.to_thread 包裹调用，
避免阻塞事件循环（快照推送/弹幕链路不受影响）。
"""
import json
import os
import sys
import time
from pathlib import Path

from hub.config import TTS_EMOTIONS
from hub.logger import get_logger
from hub.paths import BUNDLE_DIR

# 冻结后从 _MEIPASS/voice_clone 导入（打包内置）；源码运行为项目根下同目录
_VC_DIR = BUNDLE_DIR / "voice_clone"
if str(_VC_DIR) not in sys.path:
    sys.path.insert(0, str(_VC_DIR))

import minimax_client as _vc  # noqa: E402
from minimax_client import MiniMaxVoiceClient  # noqa: E402

ALLOWED_EXT = {".mp3", ".m4a", ".wav"}
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MIN_AUDIO_BYTES = 2048
VOICE_KEEPALIVE_DAYS = 7  # MiniMax：7 天未被调用即删除
# Key 连通性验证用官方公共音色合成 1 个字（与 LLM「测试并保存」同款体验）
_PROBE_VOICE_ID = "male-qn-qingse"


class WorkshopError(Exception):
    """面向控制台的可读错误（音色工坊 UI 直接展示 message）。"""


def _looks_like_audio(raw: bytes, ext: str) -> bool:
    """按扩展名检查音频魔数，防止把非音频文件传给 MiniMax（移植旧向导）。"""
    if ext == ".wav":
        return raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
    if ext == ".mp3":
        return raw[:3] == b"ID3" or (len(raw) > 2 and raw[0] == 0xFF and (raw[1] & 0xE0) == 0xE0)
    if ext == ".m4a":
        return len(raw) > 8 and raw[4:8] == b"ftyp"
    return True


def validate_voice_id(voice_id: str) -> str | None:
    """voice_id 规则校验（MiniMax 官方约束）：返回错误文案，None 即合法。"""
    vid = str(voice_id or "").strip()
    if not 8 <= len(vid) <= 256:
        return f"voice_id 需要 8~256 位（当前 {len(vid)} 位）"
    if not vid[0].isascii() or not vid[0].isalpha():
        return "voice_id 首字符必须是英文字母"
    if vid[-1] in "-_":
        return "voice_id 末位不能是 - 或 _"
    for ch in vid:
        if not (ch.isascii() and (ch.isalnum() or ch in "-_")):
            return f"voice_id 含非法字符「{ch}」（只允许字母、数字、-、_）"
    return None


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def validate_voice_params(params: dict) -> dict:
    """音色微调参数体检（语速/音量/音调/情绪），返回规范化字典；非法抛 WorkshopError。

    取值域与 MiniMax 官方一致；保存（set_voice_params）与试听（vc_preview_params）
    共用同一份校验，避免「能试不能存」。
    """
    params = params or {}
    try:
        speed = round(float(params.get("speed", 1.0)), 2)
        vol = round(float(params.get("vol", 1.0)), 1)
        pitch = int(round(float(params.get("pitch", 0))))
    except (TypeError, ValueError):
        raise WorkshopError("语速/音量/音调必须是数字")
    if not 0.5 <= speed <= 2.0:
        raise WorkshopError(f"语速需在 0.5~2.0 之间（当前 {speed}）")
    if not 0 < vol <= 10:
        raise WorkshopError(f"音量需在 0~10 之间（当前 {vol}）")
    if not -12 <= pitch <= 12:
        raise WorkshopError(f"音调需在 -12~+12 之间（当前 {pitch}）")
    emotion = str(params.get("emotion", "") or "").strip()
    if emotion and emotion not in TTS_EMOTIONS:
        raise WorkshopError(f"情绪只能是 {('/'.join(TTS_EMOTIONS))} 之一或留空（当前 {emotion}）")
    return {"speed": speed, "vol": vol, "pitch": pitch, "emotion": emotion}


# MiniMax 业务错误码 → 用户能看懂的中文（未命中保持原始英文文案）
_MINIMAX_ERRORS = {
    1008: "账户余额不足（请到 MiniMax 开放平台充值后重试）",
}


def _friendly(e: _vc.MiniMaxError) -> str:
    return _MINIMAX_ERRORS.get(e.status_code) or str(e)


class VoiceWorkshop:
    """音色工坊核心：Key 配置 / 上传 / 复刻 / 合成试听 / 音色库。"""

    def __init__(self, data_dir: str = None, client_factory=None, log=None):
        self._data_dir = Path(data_dir) if data_dir else Path(_vc.CONFIG_DIR)
        self._uploads_dir = self._data_dir / "uploads"
        self._library_file = self._data_dir / "voice_library.json"
        self._client_factory = client_factory or self._default_client_factory
        self._log = log or get_logger("voiceworkshop")

    # ---------- 客户端 ----------

    def _default_client_factory(self, api_key: str, endpoint: str) -> MiniMaxVoiceClient:
        return MiniMaxVoiceClient(api_key, endpoint=endpoint)

    def _make_client(self, api_key: str = None, endpoint: str = None) -> MiniMaxVoiceClient:
        cfg = self._read_config()
        key = str(api_key or cfg.get("api_key") or "").strip()
        if not key:
            raise WorkshopError("请先在音色工坊配置 MiniMax API Key")
        ep = str(endpoint or cfg.get("endpoint") or "").strip() or _vc.DEFAULT_ENDPOINT
        return self._client_factory(key, ep)

    # ---------- 配置文件 ----------

    def _read_config(self) -> dict:
        path = self._data_dir / "config.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def minimax_status(self) -> dict:
        cfg = self._read_config()
        key = str(cfg.get("api_key") or "")
        return {
            "configured": bool(key.strip()),
            "endpoint": str(cfg.get("endpoint") or "") or _vc.DEFAULT_ENDPOINT,
            "masked_key": (key[:6] + "****" + key[-4:]) if key else "",
        }

    def save_key(self, api_key: str, endpoint: str) -> None:
        """保存 Key/端点到 config.json（JSON 无注释保留问题）。"""
        key = str(api_key or "").strip()
        ep = str(endpoint or "").strip() or _vc.DEFAULT_ENDPOINT
        if not key:
            raise WorkshopError("API Key 不能为空")
        cfg = self._read_config()
        cfg["api_key"] = key
        cfg["endpoint"] = ep
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with open(self._data_dir / "config.json", "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except OSError as e:
            raise WorkshopError(f"保存配置失败：{e}") from e
        self._log.info("voiceworkshop_key_saved", {"endpoint": ep})

    def probe_key(self, api_key: str, endpoint: str) -> str | None:
        """Key 连通性验证：官方公共音色合成 1 字（返回可读错误，None 即通过）。"""
        try:
            client = self._make_client(api_key=api_key, endpoint=endpoint)
            client.timeout = 15  # 探测专用短超时：网络不通时快速失败，避免界面长时间「验证中」
            client.synthesize("测", voice_id=_PROBE_VOICE_ID,
                              model=_vc.DEFAULT_MODEL, format_="url")
            return None
        except _vc.MiniMaxError as e:
            return f"MiniMax 返回错误：{_friendly(e)}"
        except OSError as e:
            return f"网络请求失败：{e}"

    # ---------- 音色库 ----------

    def voices(self) -> list:
        try:
            with open(self._library_file, "r", encoding="utf-8") as f:
                lib = json.load(f)
            return lib if isinstance(lib, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _save_library(self, lib: list) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with open(self._library_file, "w", encoding="utf-8") as f:
                json.dump(lib, f, ensure_ascii=False, indent=2)
        except OSError as e:
            raise WorkshopError(f"音色库写入失败：{e}") from e

    def _touch_voice(self, voice_id: str) -> None:
        """更新最近调用时间（7 天保活：MiniMax 未被调用的音色会被删除）。"""
        lib = self.voices()
        for it in lib:
            if it.get("voice_id") == voice_id:
                it["last_called"] = _now()
        try:
            self._save_library(lib)
        except WorkshopError:
            pass  # 保活时间戳失败不阻断合成试听主流程

    # ---------- 上传 ----------

    def upload_audio(self, raw: bytes, name: str) -> dict:
        """上传待克隆音频：体检 → 落盘 uploads/ → 传 MiniMax 拿 file_id。"""
        fname = str(name or "audio.wav")
        ext = os.path.splitext(fname)[1].lower()
        if ext not in ALLOWED_EXT:
            raise WorkshopError(f"仅支持 mp3 / m4a / wav，当前为 {ext or '未知'}")
        if not raw:
            raise WorkshopError("未收到音频内容")
        if len(raw) > MAX_AUDIO_BYTES:
            raise WorkshopError(f"文件超过 20MB 上限（当前 {len(raw) // 1024 // 1024}MB）")
        if len(raw) < MIN_AUDIO_BYTES:
            raise WorkshopError(
                f"不是一个有效的音频文件（只有 {len(raw)} 字节，太小了）。"
                "这通常是因为录音/录屏软件生成了空文件或占位文件，"
                "请重新录制或换一个完整的 mp3/m4a/wav 音频（时长不少于 10 秒）。")
        if not _looks_like_audio(raw, ext):
            raise WorkshopError(
                "文件内容不是有效的音频格式（扩展名与内容不符，或已损坏）。"
                "请换一个完整、可正常播放的 mp3 / m4a / wav 文件。")
        try:
            self._uploads_dir.mkdir(parents=True, exist_ok=True)
            local = self._uploads_dir / f"{time.time():.0f}{ext}"
            local.write_bytes(raw)
        except OSError as e:
            raise WorkshopError(f"音频落盘失败：{e}") from e
        try:
            client = self._make_client()
            file_id = client.upload_audio(str(local), purpose="voice_clone")
        except _vc.MiniMaxError as e:
            raise WorkshopError(f"上传到 MiniMax 失败：{_friendly(e)}") from e
        except OSError as e:
            raise WorkshopError(f"网络请求失败：{e}") from e
        rec = {"file_id": file_id, "purpose": "voice_clone", "name": fname,
               "size": len(raw), "uploaded_at": _now(), "local_file": str(local)}
        lib = self.voices()
        lib.append(rec)
        self._save_library(lib)
        self._log.info("voiceworkshop_uploaded", {"file_id": file_id, "size": len(raw)})
        return rec

    # ---------- 复刻 ----------

    def clone_voice(self, file_id, voice_id: str, model: str = None,
                    voice_name: str = None) -> dict:
        """复刻音色并记入音色库；返回含 demo_audio 试听链接。"""
        vid = str(voice_id or "").strip()
        problem = validate_voice_id(vid)
        if problem:
            raise WorkshopError(problem)
        try:
            client = self._make_client()
            res = client.clone_voice(file_id=file_id, voice_id=vid,
                                     model=model or _vc.DEFAULT_MODEL)
        except _vc.MiniMaxError as e:
            raise WorkshopError(f"复刻失败：{_friendly(e)}") from e
        except OSError as e:
            raise WorkshopError(f"网络请求失败：{e}") from e
        rec = {
            "voice_id": vid,
            "voice_name": str(voice_name or "").strip() or vid,
            "voice_type": "voice_cloning",
            "source_file_id": file_id,
            "model": model or _vc.DEFAULT_MODEL,
            "created_at": _now(), "last_called": None,
            "demo_audio": res.get("demo_audio", ""),
        }
        lib = self.voices()
        lib.append(rec)
        self._save_library(lib)
        self._log.info("voiceworkshop_cloned", {"voice_id": vid})
        return rec

    # ---------- 合成试听 ----------

    def synthesize_wav(self, text: str, voice_id: str, model: str = None,
                       sample_rate: int = 24000, params: dict = None) -> bytes:
        """合成一段文本为 24000Hz 单声道 16bit WAV 字节（话术存档话题音频用）。

        MiniMax hex 输出裸 PCM → 本地 wave 封装——产物与播放链路（Player._decode_wav）
        参数完全一致，免二次转码。params 为该音色的微调参数（speed/vol/pitch/emotion），
        缺省读音色库档案参数，仍无则 MiniMax 默认。
        """
        import io
        import wave

        txt = str(text or "").strip()
        if not txt:
            raise WorkshopError("请输入要合成的文字")
        vid = str(voice_id or "").strip()
        if not vid:
            raise WorkshopError("请先在音色工坊克隆音色，再为话题选择音色")
        p = params if params is not None else (self.voice_params(vid) or {})
        speed = float(p.get("speed", 1.0))
        vol = float(p.get("vol", 1.0))
        pitch = int(round(float(p.get("pitch", 0))))
        emotion = str(p.get("emotion", "") or "").strip() or None
        try:
            client = self._make_client()
            res = client.synthesize(txt, voice_id=vid,
                                    model=model or _vc.DEFAULT_MODEL,
                                    format_="hex", audio_format="pcm",
                                    sample_rate=sample_rate,
                                    speed=speed, vol=vol, pitch=pitch,
                                    emotion=emotion)
        except _vc.MiniMaxError as e:
            raise WorkshopError(f"合成失败：{_friendly(e)}") from e
        except OSError as e:
            raise WorkshopError(f"网络请求失败：{e}") from e
        self._touch_voice(vid)  # 7 天保活
        hex_audio = (res.get("data") or {}).get("audio") or ""
        try:
            pcm = bytes.fromhex(hex_audio)
        except ValueError as e:
            raise WorkshopError("MiniMax 返回的音频数据无法解析") from e
        if not pcm:
            raise WorkshopError("MiniMax 未返回音频数据")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm)
        self._log.info("voiceworkshop_synthesize_wav", {"voice_id": vid,
                                                        "chars": len(txt),
                                                        "bytes": len(pcm)})
        return buf.getvalue()

    def preview(self, text: str, voice_id: str, model: str = None) -> dict:
        """用克隆音色合成一段试听（url 直链，控制台浏览器播放，不走直播声卡）。"""
        txt = str(text or "").strip()
        if not txt:
            raise WorkshopError("请输入要合成的文字")
        vid = str(voice_id or "").strip()
        if not vid:
            raise WorkshopError("请选择或填入音色 voice_id")
        try:
            client = self._make_client()
            res = client.synthesize(txt, voice_id=vid,
                                    model=model or _vc.DEFAULT_MODEL, format_="url")
        except _vc.MiniMaxError as e:
            raise WorkshopError(f"合成失败：{_friendly(e)}") from e
        except OSError as e:
            raise WorkshopError(f"网络请求失败：{e}") from e
        self._touch_voice(vid)  # 正式合成调用记入 last_called（7 天保活）
        audio = (res.get("data") or {}).get("audio") or ""
        if not audio:
            raise WorkshopError("MiniMax 未返回音频链接")
        self._log.info("voiceworkshop_preview", {"voice_id": vid, "chars": len(txt)})
        return {"audio_url": audio, "voice_id": vid, "chars": len(txt)}

    # ---------- 账号音色拉取（一键获取 API Key 下全部音色） ----------

    def fetch_account_voices(self) -> dict:
        """拉取 API Key 账号下的复刻/文生音色并合入音色库（按 voice_id 去重）。

        系统音色不导入（官方预置、不过期、随时可用），只回报数量；
        官方限制：复刻/文生音色需成功合成过一次才会出现在接口返回里。
        """
        try:
            client = self._make_client()
            res = client.get_voice("all")
        except _vc.MiniMaxError as e:
            raise WorkshopError(f"获取账号音色失败：{_friendly(e)}") from e
        except OSError as e:
            raise WorkshopError(f"网络请求失败：{e}") from e

        lib = self.voices()
        known = {it.get("voice_id") for it in lib if it.get("voice_id")}
        added = []
        dirty = False
        for group in ("voice_cloning", "voice_generation"):
            for v in res.get(group) or []:
                vid = str(v.get("voice_id") or "").strip()
                if not vid:
                    continue
                if vid in known:
                    # 老记录补 voice_type（删除接口需要区分克隆/文生）
                    rec0 = next((it for it in lib if it.get("voice_id") == vid), None)
                    if rec0 is not None and not rec0.get("voice_type"):
                        rec0["voice_type"] = group
                        dirty = True
                    continue
                known.add(vid)
                rec = {"voice_id": vid, "voice_name": vid,
                       "source": "account_fetch", "voice_type": group,
                       "created_at": str(v.get("created_time") or "") or _now(),
                       "last_called": None}
                lib.append(rec)
                added.append(rec)
        if added or dirty:
            self._save_library(lib)
        system_count = len(res.get("system_voice") or [])
        self._log.info("voiceworkshop_fetch_account_voices",
                       {"added": len(added), "system": system_count})
        return {"added": added, "system_count": system_count, "voices": lib}

    # ---------- 删除账号音色（平台侧真实删除，不可恢复） ----------

    def delete_voice(self, voice_id: str, live_voice_id: str = None) -> dict:
        """删除 MiniMax 账号下的音色并移出本地音色库。

        安全闸：live_voice_id 传入当前直播音色时，若二者相同直接拒绝
        （先切换直播音色再删）；语音合成按 voice_id 引用，误删直播音色
        会导致开播后 TTS 全部失败。
        """
        vid = str(voice_id or "").strip()
        if not vid:
            raise WorkshopError("缺少 voice_id")
        if live_voice_id and vid == str(live_voice_id).strip():
            raise WorkshopError(f"「{vid}」是当前直播音色，请先在音色库切换其他音色再删除")
        lib = self.voices()
        rec = next((it for it in lib if it.get("voice_id") == vid), None)
        if rec is None:
            raise WorkshopError(f"音色库中不存在音色 {vid}")
        # 老记录可能没有 voice_type：复刻工具创建的都是 voice_cloning
        vtype = str(rec.get("voice_type") or "") or "voice_cloning"
        if vtype not in ("voice_cloning", "voice_generation"):
            raise WorkshopError(f"音色类型 {vtype} 不支持删除（仅克隆/文生音色可删）")
        try:
            client = self._make_client()
            client.delete_voice(vid, vtype)
        except _vc.MiniMaxError as e:
            raise WorkshopError(f"删除失败：{_friendly(e)}") from e
        except OSError as e:
            raise WorkshopError(f"网络请求失败：{e}") from e
        lib = [it for it in lib if it.get("voice_id") != vid]
        self._save_library(lib)
        self._log.info("voiceworkshop_voice_deleted",
                       {"voice_id": vid, "voice_type": vtype})
        return {"voice_id": vid, "voice_type": vtype, "voices": lib}

    # ---------- 音色参数档案（语速/音量/音调/情绪，随音色走） ----------

    def voice_params(self, voice_id: str) -> dict | None:
        """读某音色的微调参数；未设置返回 None（调用方保持现有默认）。"""
        vid = str(voice_id or "").strip()
        for it in self.voices():
            if it.get("voice_id") == vid:
                p = it.get("params")
                return dict(p) if isinstance(p, dict) else None
        return None

    def set_voice_params(self, voice_id: str, params: dict) -> dict:
        """保存某音色的微调参数到音色库（热生效由 main 的热配置通道负责）。"""
        vid = str(voice_id or "").strip()
        if not vid:
            raise WorkshopError("缺少 voice_id")
        cleaned = validate_voice_params(params)
        lib = self.voices()
        rec = next((it for it in lib if it.get("voice_id") == vid), None)
        if rec is None:
            raise WorkshopError(f"音色库中不存在音色 {vid}")
        rec["params"] = cleaned
        self._save_library(lib)
        self._log.info("voiceworkshop_params_saved", {"voice_id": vid, **cleaned})
        return rec
