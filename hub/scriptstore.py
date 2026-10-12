"""话术存档库（话术管理 v4：单方案纯AI + 存档内多音色 × 多情绪档）。

存档 = profiles/<存档id>/ 目录：profile.yaml（全部文本数据）+ audio/（音频资产）。
- 结构 v4：模板方案整体退役，只剩 AI 分区；每条话术（话题/样本）的音频按
  「音色 × 情绪档」二维存放（audios: {voice_id: {情绪档: {file, text_hash}}}），
  整档可挂多个音色，current_voice 指向当前直播音色，切换即热生效：
    voices: [{voice_id, label, params}]   存档音色列表（params 可空 {}）
    current_voice: <voice_id>             当前直播音色
    emotions: [档位…]                     情绪档位集合（default 恒在，其余取 8 枚举）
    samples_ai / topics_ai                弹幕样本 / 话题（audios 多音色多情绪映射）
- 共用区块：knowledge 游戏知识库 / event_templates 事件模板库 / schedule 轮播调度
- 存档 id 程序生成（目录名），显示名在 yaml 的 name 字段——重命名不动目录
- config.yaml 只记 script.active_profile 指针（行级写回在 hub.config.update_script_config）

情绪档位（DESIGN §8）：MiniMax 情绪枚举 8 个 + 特殊档 `default`（恒存、不可删，
承载 v3 时期的扁平音频与音色档案参数）。同一文本 × 不同情绪 = 不同音频文件，
故「改文本」会让该音色**全部档位**一起标 stale。

text_hash = sha1(文本.strip())[:8]：音频与文本的对应凭据；条目文本改过后
旧音频即「过期」（stale）——stale 不写盘，get_profile 返回时动态计算，
给出 voice 级 `stale`（任一档位失配）与逐档位明细 `stale_emotions`；
stale 音频不播放（运行侧回退现场 TTS 念新文本），前端显示「文本已改，需重新生成」。

迁移（启动时调用一次，顺序 v1 → v2 → v3）：
- v1 → 最新（migrate_v1_profiles）：清空样本/话题库并删除孤儿音频（用户决策：清空重建）。
- v2 → 最新（migrate_v2_profiles）：旧 audio+voice_id → audios[voice_id] 映射；
  从条目收集 voices（label 查音色工坊库）；current_voice = config tts.voice_id
  （在 voices 内）否则第一个音色；template 两分区及其音频文件删除（模板方案退役）。
- v3 → v4（migrate_v3_profiles）：audios[voice_id] 的扁平 {file, text_hash}
  包成 {"default": 原值}，补 emotions 档位集合，version 置 4；
  迁移前备份 profile.yaml 为 profile.yaml.v3.bak，且**幂等**（不二次包装）。

音频约束复用音色工坊：mp3/m4a/wav ≤20MB、≥2048 字节、按扩展名魔数校验。
导出/导入为 zip（profile.yaml + audio/*）：整包可搬到另一台电脑直接使用。
导入一律落为「新存档」（新 id），永不覆盖现有存档；v2 旧包导入时按同一规则转 v3。
"""
import hashlib
import re
import secrets
import shutil
import zipfile
from pathlib import Path

import yaml

from hub.config import TTS_EMOTIONS
from hub.logger import get_logger
from hub.paths import BUNDLE_DIR, EXE_DIR
from hub.templates import GIFT_TEMPLATES, WELCOME_TEMPLATES
from hub.voiceworkshop import (
    ALLOWED_EXT,
    MAX_AUDIO_BYTES,
    MIN_AUDIO_BYTES,
    _looks_like_audio,
)

PROFILE_FILE = "profile.yaml"
BACKUP_SUFFIX_V3 = ".v3.bak"
AUDIO_DIR = "audio"
PROFILE_VERSION = 4
# 存档内的条目分区（v3 起）：模板分区已随方案退役删除
SECTIONS = ("samples_ai", "topics_ai")
# 情绪档位：default 恒在（承载 v3 扁平音频），其余取 MiniMax 8 枚举（hub.config.TTS_EMOTIONS）
EMOTION_DEFAULT = "default"
EMOTIONS_ALL = (EMOTION_DEFAULT,) + tuple(TTS_EMOTIONS)
# 存档出厂档位集合（DESIGN §8.2）：default 必含，happy/calm 为用户可取消的预生成档
DEFAULT_EMOTIONS = (EMOTION_DEFAULT, "happy", "calm")
DEFAULT_SCHEDULE = {"enabled": True, "interval_s": 30, "jitter_s": 15}
# 事件模板库 6 类事件的开关：enter 默认关（与 config events.enter.enabled 默认一致）
EVENT_KEYS = ("gift_low", "gift_mid", "gift_high", "follow", "fansclub", "enter")
DEFAULT_EVENT_ENABLED = {"gift_low": True, "gift_mid": True, "gift_high": True,
                         "follow": True, "fansclub": True, "enter": False}
# 音色微调参数缺省值（params 留空 = 跟随音色工坊档案参数或此处默认）
DEFAULT_VOICE_PARAMS = {"speed": 1.0, "vol": 1.0, "pitch": 0, "emotion": ""}

_PID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# 存档音频文件名：程序生成的 token+扩展名；导入时也按此收紧（防路径穿越）
_AUDIO_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\.[A-Za-z0-9]{2,4}")


class ScriptStoreError(Exception):
    """面向控制台的可读错误（话术管理 UI 直接展示 message）。"""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def text_hash(text: str) -> str:
    """条目文本的内容指纹：音频与文本的对应凭据（改文本即判旧音频过期）。"""
    return hashlib.sha1(str(text or "").strip().encode("utf-8")).hexdigest()[:8]


def default_event_templates() -> dict:
    """新建存档的初始事件模板：取 hub.templates 现行文案（礼物三档/欢迎三组）。"""
    return {
        "gift_low": list(GIFT_TEMPLATES["low"]),
        "gift_mid": list(GIFT_TEMPLATES["mid"]),
        "gift_high": list(GIFT_TEMPLATES["high"]),
        "follow": list(WELCOME_TEMPLATES["follow"]),
        "fansclub": list(WELCOME_TEMPLATES["fansclub"]),
        "enter": list(WELCOME_TEMPLATES["enter"]),
    }


def default_profile(name: str) -> dict:
    return {
        "version": PROFILE_VERSION,
        "name": str(name or "").strip() or "未命名存档",
        "voices": [],
        "current_voice": "",
        "emotions": list(DEFAULT_EMOTIONS),
        "knowledge": {
            "nickname": "仔仔鼠",
            "style": "口语化、热情、短句",
            "max_reply_chars": 40,
            "game_name": "",
            "game_intro": "",
            "download_hint": "",
            "highlights": [],
        },
        "samples_ai": [],
        "event_templates": default_event_templates(),
        "event_enabled": default_event_enabled(),
        "topics_ai": [],
        "schedule": dict(DEFAULT_SCHEDULE),
    }


# ---------- 归一化（加载与保存共用：任何脏数据进内存前都过一遍） ----------

def _s(value, default: str = "") -> str:
    text = str(value if value is not None else default).strip()
    return text if text else default


def _str_list(value) -> list:
    if isinstance(value, (list, tuple)):
        items = value
    elif value is None:
        items = []
    else:
        items = [value]
    return [str(x).strip() for x in items if str(x or "").strip()]


def _bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
    return default


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _float(value, default: float, lo: float, hi: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        f = default
    return max(lo, min(hi, f))


def _audio_ref(value) -> str:
    """yaml 里的音频引用只接受程序生成的文件名（basename，扩展名合法）。"""
    name = str(value or "").strip()
    if not name:
        return ""
    if not _AUDIO_NAME_RE.fullmatch(name):
        return ""
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        return ""
    return name


def normalize_voice_params(data) -> dict:
    """音色微调参数归一：只收 speed/vol/pitch/emotion，静默钳到合法域（不抛错）。"""
    d = data if isinstance(data, dict) else {}
    out = {}
    if "speed" in d:
        out["speed"] = _float(d.get("speed"), 1.0, 0.5, 2.0)
    if "vol" in d:
        out["vol"] = _float(d.get("vol"), 1.0, 0.1, 10.0)
    if "pitch" in d:
        out["pitch"] = _int(d.get("pitch"), 0, -12, 12)
    if "emotion" in d:
        out["emotion"] = _s(d.get("emotion"))
    return out


def normalize_voice(data) -> dict | None:
    d = data if isinstance(data, dict) else {}
    voice_id = _s(d.get("voice_id"))
    if not voice_id:
        return None
    return {"voice_id": voice_id,
            "label": _s(d.get("label")) or voice_id,
            "params": normalize_voice_params(d.get("params"))}


def normalize_emotion(value) -> str:
    """单值情绪归一：留空 / 不在 8 枚举内 → `default`（永不抛错）。"""
    e = str(value or "").strip()
    return e if e in EMOTIONS_ALL else EMOTION_DEFAULT


def normalize_emotions(data) -> list:
    """情绪档位集合归一：default 恒在且排首位，其余按 8 枚举顺序排列，非法档丢弃。

    缺键（v3 存档、手改残缺）→ 出厂默认 default + happy + calm；
    显式空列表 → 只剩 default（default 不可取消）。
    """
    if data is None:
        picked = set(DEFAULT_EMOTIONS)
    else:
        picked = {e for e in _str_list(data) if e in EMOTIONS_ALL}
    out = [EMOTION_DEFAULT]
    for e in TTS_EMOTIONS:
        if e in picked and e not in out:
            out.append(e)
    return out


def _flat_to_slots(entry: dict) -> dict:
    """v3 扁平 {file, text_hash} → {"default": {...}}；已是 v4 形态则原样返回。

    这是 v3 → v4 的唯一判定点（「该音色的值里是否直接含 file 字段」）：
    读取时用它把旧结构包成新结构，落盘迁移用同一条规则，故迁移天然幂等——
    包装后的值里 `file` 位于情绪档之下，不会命中本判定，不会二次包装。
    """
    if isinstance(entry.get("file"), str) and str(entry.get("file") or "").strip():
        return {EMOTION_DEFAULT: {"file": entry.get("file"),
                                  "text_hash": entry.get("text_hash")}}
    return {str(k): v for k, v in entry.items() if isinstance(v, dict)}


def _has_flat_audios(raw: dict) -> bool:
    """raw 存档数据里是否还残留 v3 扁平 audios 形态（某音色的值里直接含 file）。

    供 migrate_v3_profiles 判定「version 已写成 4 但内容仍是旧结构」的存档
    （手改 yaml / 迁移中断），保证这类存档同样会被真正迁移。
    """
    for key in SECTIONS:
        for item in _as_list((raw or {}).get(key)):
            if not isinstance(item, dict):
                continue
            for entry in (item.get("audios") or {}).values():
                if (isinstance(entry, dict) and isinstance(entry.get("file"), str)
                        and str(entry.get("file") or "").strip()):
                    return True
    return False


def normalize_audios(data) -> dict:
    """audios 归一（v4）：{voice_id: {情绪档: {file, text_hash}}}；非法引用直接丢弃。

    兼容 v3 扁平形态（自动包成 default 档），故内存结构恒为二维映射。
    """
    d = data if isinstance(data, dict) else {}
    out = {}
    for vid, entry in d.items():
        vid = str(vid or "").strip()
        if not vid or not isinstance(entry, dict):
            continue
        slots = {}
        for emo, ref in _flat_to_slots(entry).items():
            if emo not in EMOTIONS_ALL or not isinstance(ref, dict):
                continue
            file = _audio_ref(ref.get("file"))
            if not file:
                continue
            slots[emo] = {"file": file, "text_hash": _s(ref.get("text_hash"))}
        if slots:
            out[vid] = slots
    return out


def audio_slots(item: dict, voice_id: str) -> dict:
    """条目某音色的情绪档映射（无则空 dict）；供调用方只读取用。"""
    if not isinstance(item, dict):
        return {}
    slots = (item.get("audios") or {}).get(str(voice_id or "").strip())
    return slots if isinstance(slots, dict) else {}


def audio_slot(item: dict, voice_id: str, emotion: str = "") -> dict:
    """条目「音色 × 情绪档」的音频引用（无则空 dict）。emotion 留空 → default。"""
    emo = str(emotion or "").strip() or EMOTION_DEFAULT
    ref = audio_slots(item, voice_id).get(emo)
    return ref if isinstance(ref, dict) else {}


def normalize_knowledge(data) -> dict:
    d = data if isinstance(data, dict) else {}
    return {
        "nickname": _s(d.get("nickname"), "仔仔鼠"),
        "style": _s(d.get("style"), "口语化、热情、短句"),
        "max_reply_chars": _int(d.get("max_reply_chars"), 40, 20, 60),
        "game_name": _s(d.get("game_name")),
        "game_intro": _s(d.get("game_intro")),
        "download_hint": _s(d.get("download_hint")),
        "highlights": _str_list(d.get("highlights")),
    }


def normalize_sample(data) -> dict:
    d = data if isinstance(data, dict) else {}
    return {
        "id": _s(d.get("id")) or _new_id("s"),
        "type_name": _s(d.get("type_name")),
        "examples": _str_list(d.get("examples")),
        "reply_text": _s(d.get("reply_text")),
        "audios": normalize_audios(d.get("audios")),
        "enabled": _bool(d.get("enabled"), True),
    }


def normalize_topic(data) -> dict:
    d = data if isinstance(data, dict) else {}
    return {
        "id": _s(d.get("id")) or _new_id("t"),
        "title": _s(d.get("title")),
        "text": _s(d.get("text")),
        "audios": normalize_audios(d.get("audios")),
        "enabled": _bool(d.get("enabled"), True),
    }


def normalize_event_templates(data) -> dict:
    """缺键补内置默认文案：导入旧存档/手改残缺时事件话术仍开箱可用。"""
    d = data if isinstance(data, dict) else {}
    defaults = default_event_templates()
    out = {}
    for key, fallback in defaults.items():
        items = _str_list(d.get(key))
        out[key] = items if items else list(fallback)
    return out


def default_event_enabled() -> dict:
    """事件开关默认值：礼物/关注/粉丝团默认开，进场欢迎默认关。"""
    return dict(DEFAULT_EVENT_ENABLED)


def normalize_event_enabled(data) -> dict:
    """事件开关归一化：缺键回退默认值（导入旧存档/手改残缺时行为稳定）。"""
    d = data if isinstance(data, dict) else {}
    defaults = default_event_enabled()
    return {key: _bool(d.get(key), defaults[key]) for key in EVENT_KEYS}


def normalize_schedule(data) -> dict:
    d = data if isinstance(data, dict) else {}
    return {
        "enabled": _bool(d.get("enabled"), DEFAULT_SCHEDULE["enabled"]),
        "interval_s": _int(d.get("interval_s"), DEFAULT_SCHEDULE["interval_s"], 1, 3600),
        "jitter_s": _int(d.get("jitter_s"), DEFAULT_SCHEDULE["jitter_s"], 0, 600),
    }


def _normalize_voices(data) -> list:
    out = []
    seen = set()
    for v in _as_list(data):
        voice = normalize_voice(v)
        if voice is not None and voice["voice_id"] not in seen:
            seen.add(voice["voice_id"])
            out.append(voice)
    return out


def normalize_profile(data) -> dict:
    """归一化存档（v4：单方案 + 多音色 × 多情绪档 audios 映射）。

    v1/v2 旧结构在落盘前先经 migrate_* 转换（启动时一次性）；v3 的扁平 audios
    由 normalize_audios 就地包成 default 档，故内存结构唯一。直接读到的旧字段
    （samples/topics 顶层键、template 分区、条目级 audio/voice_id/keywords/topic_id）
    一律忽略——normalize 不兼旧。
    """
    if not isinstance(data, dict):
        raise ScriptStoreError("profile.yaml 顶层必须是键值映射")
    voices = _normalize_voices(data.get("voices"))
    current = _s(data.get("current_voice"))
    if current and voices and current not in {v["voice_id"] for v in voices}:
        current = ""
    return {
        "version": PROFILE_VERSION,
        "name": _s(data.get("name"), "未命名存档"),
        "voices": voices,
        "current_voice": current,
        "emotions": normalize_emotions(data.get("emotions")),
        "knowledge": normalize_knowledge(data.get("knowledge")),
        "samples_ai": [normalize_sample(x) for x in _as_list(data.get("samples_ai"))],
        "event_templates": normalize_event_templates(data.get("event_templates")),
        "event_enabled": normalize_event_enabled(data.get("event_enabled")),
        "topics_ai": [normalize_topic(x) for x in _as_list(data.get("topics_ai"))],
        "schedule": normalize_schedule(data.get("schedule")),
    }


def _as_list(value) -> list:
    return [x for x in value] if isinstance(value, (list, tuple)) else []


def _item_text(item: dict) -> str:
    """条目用于 text_hash 的正文：样本取 reply_text，话题取 text。"""
    return _s(item.get("reply_text")) if "reply_text" in item else _s(item.get("text"))


def count_missing(profile: dict, voice_id: str = "", exists=None) -> dict:
    """缺失统计（纯数据）：{"voice_id","total","missing": {档位: 缺失条数}}。

    统计口径：启用且文本非空的条目（total），逐「档位」判定该音色是否缺音频——
    无引用 / text_hash 与当前文本不符 / `exists(name)` 为假（文件不在），都算缺。
    档位集合取自存档 emotions（default 恒在）；无音色时 missing 为空 dict。
    """
    p = profile or {}
    vid = _s(voice_id) or _s(p.get("current_voice"))
    emotions = normalize_emotions(p.get("emotions")) if vid else []
    missing = {e: 0 for e in emotions}
    total = 0
    for key in SECTIONS:
        for item in _as_list(p.get(key)):
            if not isinstance(item, dict) or not item.get("enabled"):
                continue
            text = _item_text(item)
            if not text:
                continue
            total += 1
            want = text_hash(text)
            for emo in emotions:
                ref = audio_slot(item, vid, emo)
                name = _s(ref.get("file"))
                if not name or _s(ref.get("text_hash")) != want \
                        or (exists is not None and not exists(name)):
                    missing[emo] += 1
    return {"voice_id": vid, "total": total, "missing": missing}


def _mark_stale(items: list) -> None:
    """给条目动态标注过期音频（音频 text_hash ≠ 当前文本 hash）。

    两个字段：
    - `stale`：voice 级——该音色只要有**任一**档位失配就上榜（前端旧语义，兼容保留）
    - `stale_emotions`：逐档位明细 {voice_id: [失配档位…]}，供情绪档位区精确定位
    同一文本 × 不同情绪 = 不同音频，故改文本会让该音色的全部档位一起过期。
    """
    for item in items:
        current = text_hash(_item_text(item))
        by_voice = {}
        for vid, slots in (item.get("audios") or {}).items():
            if not isinstance(slots, dict):
                continue
            bad = sorted(emo for emo, ref in slots.items()
                         if not isinstance(ref, dict)
                         or str(ref.get("text_hash") or "") != current)
            if bad:
                by_voice[vid] = bad
        item["stale"] = sorted(by_voice)
        item["stale_emotions"] = by_voice


def _v2_to_v3(raw: dict, tts_voice_id: str = "", voice_labels: dict = None) -> tuple:
    """v2 存档数据 → v3 结构（纯数据转换，不碰磁盘）。

    返回 (v3_dict, dropped_audio_names)：旧 audio+voice_id → audios 映射；从条目
    收集 voices（label 查音色工坊库，未入库用 voice_id 顶）；current_voice =
    tts.voice_id（在 voices 内）否则第一个音色；template 两分区与条目级
    keywords/topic_id 丢弃，其音频文件名进 dropped_audio_names 由调用方处理。
    """
    labels = voice_labels or {}
    tts_voice_id = str(tts_voice_id or "").strip()
    voices: list = []
    seen: set = set()
    dropped: list = []

    def _collect(vid: str) -> None:
        if vid and vid not in seen:
            seen.add(vid)
            voices.append({"voice_id": vid,
                           "label": str(labels.get(vid) or vid), "params": {}})

    def _migrate_items(items, text_key: str) -> list:
        out = []
        for it in _as_list(items):
            if not isinstance(it, dict):
                continue
            vid = _s(it.get("voice_id"))
            audio = _s(it.get("audio"))
            if vid:
                _collect(vid)
            audios = {}
            if audio and vid:
                audios[vid] = {"file": audio, "text_hash": text_hash(it.get(text_key))}
            elif audio:
                # 有音频但无音色归属：无法归入任何音色，按孤儿音频丢弃
                dropped.append(audio)
            out.append({
                "id": _s(it.get("id")),
                **({"type_name": _s(it.get("type_name")),
                    "examples": _str_list(it.get("examples")),
                    "reply_text": _s(it.get("reply_text"))}
                   if text_key == "reply_text" else
                   {"title": _s(it.get("title")), "text": _s(it.get("text"))}),
                "audios": audios,
                "enabled": _bool(it.get("enabled"), True),
            })
        return out

    for key in ("samples_template", "topics_template"):
        for it in _as_list(raw.get(key)):
            if isinstance(it, dict) and _s(it.get("audio")):
                dropped.append(_s(it.get("audio")))

    samples = _migrate_items(raw.get("samples_ai"), "reply_text")
    topics = _migrate_items(raw.get("topics_ai"), "text")
    current = tts_voice_id if tts_voice_id in seen else (
        voices[0]["voice_id"] if voices else "")
    data = {
        "version": PROFILE_VERSION,
        "name": raw.get("name"),
        "voices": voices,
        "current_voice": current,
        "emotions": list(DEFAULT_EMOTIONS),
        "knowledge": raw.get("knowledge"),
        "samples_ai": samples,
        "topics_ai": topics,
        "event_templates": raw.get("event_templates"),
        "event_enabled": raw.get("event_enabled"),
        "schedule": raw.get("schedule"),
    }
    return data, dropped


class ScriptStore:
    """存档 CRUD + 音色管理 + 音频资产 + zip 导入导出。root 默认 EXE_DIR/profiles。"""

    def __init__(self, root=None, log=None):
        self._root = Path(root) if root else (EXE_DIR / "profiles")
        self._log = log or get_logger("scriptstore")
        self._synth = None   # 音频合成器注入位（set_synthesizer），未注入时 generate_audio 报错

    @property
    def root(self) -> Path:
        return self._root

    # ---------- 基础 ----------

    def _check_pid(self, pid: str) -> str:
        pid = str(pid or "").strip()
        if not _PID_RE.fullmatch(pid):
            raise ScriptStoreError(f"存档 id 非法：{pid!r}")
        return pid

    def _dir(self, pid: str) -> Path:
        return self._root / self._check_pid(pid)

    def exists(self, pid: str) -> bool:
        try:
            return (self._dir(pid) / PROFILE_FILE).is_file()
        except ScriptStoreError:
            return False

    def _read_yaml(self, pid: str) -> dict:
        path = self._dir(pid) / PROFILE_FILE
        if not path.is_file():
            raise ScriptStoreError(f"存档不存在：{pid}")
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            raise ScriptStoreError(f"存档文件解析失败：{e}") from e
        except OSError as e:
            raise ScriptStoreError(f"存档文件读取失败：{e}") from e
        return data

    def _write_yaml(self, pid: str, data: dict) -> None:
        pdir = self._dir(pid)
        try:
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / AUDIO_DIR).mkdir(exist_ok=True)
            text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
            (pdir / PROFILE_FILE).write_text(text, encoding="utf-8")
        except OSError as e:
            raise ScriptStoreError(f"存档写入失败：{e}") from e

    # ---------- 存档 CRUD ----------

    def list_profiles(self) -> list:
        """存档摘要列表（按修改时间倒序）。损坏的存档带 error 标记，不静默隐藏。"""
        if not self._root.is_dir():
            return []
        out = []
        for pdir in self._root.iterdir():
            if not pdir.is_dir() or not (pdir / PROFILE_FILE).is_file():
                continue
            entry = {"id": pdir.name, "name": "", "updated":
                     (pdir / PROFILE_FILE).stat().st_mtime, "error": False}
            try:
                data = normalize_profile(self._read_yaml(pdir.name))
                entry.update({"name": data["name"],
                              "topics_ai": len(data["topics_ai"]),
                              "samples_ai": len(data["samples_ai"]),
                              "voices": len(data["voices"]),
                              "current_voice": data["current_voice"]})
            except ScriptStoreError as e:
                entry["name"] = "<读取失败>"
                entry["error"] = True
                self._log.warning("profile_list_broken", {"id": pdir.name, "error": str(e)})
            out.append(entry)
        out.sort(key=lambda x: -x["updated"])
        return out

    def get_profile(self, pid: str) -> dict:
        """加载并归一化（只读不写盘）；返回 dict 带 id 字段，条目动态标注 stale。"""
        data = normalize_profile(self._read_yaml(pid))
        _mark_stale(data["samples_ai"])
        _mark_stale(data["topics_ai"])
        return {"id": self._check_pid(pid), **data}

    def _guard_existing_audio(self, pid: str, data: dict) -> dict:
        """防覆盖已生成音频：前端整档保存若把某条目的 audios 条目冲丢，
        但磁盘上同一条目同一音色同一情绪档已有音频且文件仍存在，则保留磁盘引用。

        根因：某次 sc_save 携带空的 audios 会把磁盘上已生成的音频引用覆盖掉，
        刷新后音频"消失"。显式清除不受影响——清除走删文件路径，此处检测文件
        不存在即放行。

        v4 语义：判定粒度是「音色 × 情绪档」，逐档位各自守卫（丢一档不牵连其余档）。
        """
        if not isinstance(data, dict):
            return data
        try:
            disk = self._read_yaml(pid)
        except (ScriptStoreError, OSError):
            return data
        for key in SECTIONS:
            items = data.get(key)
            if not isinstance(items, list):
                continue
            cur = {}
            for it in (disk or {}).get(key, []) or []:
                if isinstance(it, dict) and it.get("id"):
                    cur[it["id"]] = it.get("audios") or {}
            for it in items:
                if not isinstance(it, dict) or not it.get("id"):
                    continue
                old_audios = cur.get(it["id"]) or {}
                if not isinstance(old_audios, dict):
                    continue
                new_audios = it.setdefault("audios", {})
                if not isinstance(new_audios, dict):
                    it["audios"] = new_audios = {}
                for vid, entry in old_audios.items():
                    if not isinstance(entry, dict):
                        continue
                    slots = _flat_to_slots(entry)
                    kept = new_audios.get(vid)
                    if not isinstance(kept, dict):
                        kept = new_audios[vid] = {}
                    for emo, ref in slots.items():
                        if not isinstance(ref, dict):
                            continue
                        name = str(ref.get("file") or "").strip()
                        have = kept.get(emo)
                        have_file = (isinstance(have, dict)
                                     and str(have.get("file") or "").strip())
                        if name and not have_file and self.audio_exists(pid, name):
                            kept[emo] = {"file": name,
                                         "text_hash": _s(ref.get("text_hash"))}
        return data

    def save_profile(self, pid: str, data: dict, *, guard: bool = True) -> dict:
        """整存归一化写盘（页面编辑的唯一持久化入口）；返回归一化后的 dict（带 stale 标注）。

        guard=False 跳过防覆盖护栏：remove_voice 等内部操作显式删除 audios 条目时使用
        （护栏会把「条目缺失但文件仍在」的引用还原，与显式移除音色的语义冲突）。
        """
        if guard:
            data = self._guard_existing_audio(pid, data)
        normalized = normalize_profile(data)
        self._write_yaml(pid, normalized)
        self._log.info("profile_saved", {"id": pid, "name": normalized["name"]})
        _mark_stale(normalized["samples_ai"])
        _mark_stale(normalized["topics_ai"])
        return {"id": self._check_pid(pid), **normalized}

    def create_profile(self, name: str, source_id: str = None) -> dict:
        """新建存档：source_id 给定时完整复制其文本与音频（另起新 id）。"""
        pid = _new_id("p")
        if source_id:
            src = self.get_profile(source_id)
            data = {k: src[k] for k in
                    ("version", "name", "voices", "current_voice", "emotions",
                     "knowledge", "event_templates", "event_enabled", "schedule",
                     "samples_ai", "topics_ai")}
            data["name"] = _s(name, src["name"] + " 副本")
            self._write_yaml(pid, normalize_profile(data))
            src_audio = self._dir(source_id) / AUDIO_DIR
            if src_audio.is_dir():
                shutil.copytree(src_audio, self._dir(pid) / AUDIO_DIR, dirs_exist_ok=True)
        else:
            self._write_yaml(pid, default_profile(name))
        self._log.info("profile_created", {"id": pid, "source": source_id or None})
        return self.get_profile(pid)

    def rename_profile(self, pid: str, name: str) -> dict:
        data = self.get_profile(pid)
        data["name"] = _s(name) or data["name"]
        return self.save_profile(pid, data)

    def delete_profile(self, pid: str) -> None:
        pdir = self._dir(pid)
        if not (pdir / PROFILE_FILE).is_file():
            raise ScriptStoreError(f"存档不存在：{pid}")
        shutil.rmtree(pdir)
        self._log.info("profile_deleted", {"id": pid})

    def ensure_default(self) -> str:
        """无存档时创建「默认话术」；返回一个可用存档 id（启动兜底用）。"""
        existing = [p for p in self.list_profiles() if not p.get("error")]
        if existing:
            return existing[0]["id"]
        profile = self.create_profile("默认话术")
        self._log.info("profile_default_created", {"id": profile["id"]})
        return profile["id"]

    # ---------- 存档音色管理 ----------

    def add_voice(self, pid: str, voice_id: str, label: str = "") -> dict:
        """把音色工坊库存音色挂入存档；首个音色自动成为当前直播音色。"""
        voice_id = _s(voice_id)
        if not voice_id:
            raise ScriptStoreError("音色 ID 不能为空")
        profile = self.get_profile(pid)
        if any(v["voice_id"] == voice_id for v in profile["voices"]):
            raise ScriptStoreError(f"音色 {voice_id} 已在存档音色列表中")
        profile["voices"].append({"voice_id": voice_id,
                                  "label": _s(label) or voice_id, "params": {}})
        if not profile["current_voice"]:
            profile["current_voice"] = voice_id
        saved = self.save_profile(pid, profile)
        self._log.info("profile_voice_added", {"id": pid, "voice_id": voice_id})
        return saved

    def remove_voice(self, pid: str, voice_id: str) -> dict:
        """从存档移除音色：连带删除该音色**全部情绪档**音频文件；当前直播音色拒删。"""
        voice_id = _s(voice_id)
        profile = self.get_profile(pid)
        if profile["current_voice"] == voice_id:
            raise ScriptStoreError("该音色是当前直播音色，请先切换直播音色再移除")
        if not any(v["voice_id"] == voice_id for v in profile["voices"]):
            raise ScriptStoreError(f"音色 {voice_id} 不在存档音色列表中")
        profile["voices"] = [v for v in profile["voices"]
                             if v["voice_id"] != voice_id]
        removed_files = []
        for key in SECTIONS:
            for item in profile[key]:
                slots = (item.get("audios") or {}).pop(voice_id, None)
                if isinstance(slots, dict):
                    removed_files += [str(ref.get("file") or "")
                                      for ref in slots.values()
                                      if isinstance(ref, dict)]
        saved = self.save_profile(pid, profile, guard=False)
        for name in removed_files:
            self.delete_audio(pid, name)
        self._log.info("profile_voice_removed", {"id": pid, "voice_id": voice_id,
                                                 "removed_audio": len(removed_files)})
        return saved

    def set_current_voice(self, pid: str, voice_id: str) -> dict:
        """切换存档当前直播音色（必须在 voices 列表内）。"""
        voice_id = _s(voice_id)
        profile = self.get_profile(pid)
        if voice_id and voice_id not in {v["voice_id"] for v in profile["voices"]}:
            raise ScriptStoreError(f"音色 {voice_id} 不在存档音色列表中，请先添加")
        profile["current_voice"] = voice_id
        saved = self.save_profile(pid, profile)
        self._log.info("profile_current_voice", {"id": pid, "voice_id": voice_id})
        return saved

    def update_voice(self, pid: str, voice_id: str,
                     label: str = None, params: dict = None) -> dict:
        """改存档音色的备注名 / 微调参数（None 的字段不动）。"""
        voice_id = _s(voice_id)
        profile = self.get_profile(pid)
        voice = next((v for v in profile["voices"] if v["voice_id"] == voice_id), None)
        if voice is None:
            raise ScriptStoreError(f"音色 {voice_id} 不在存档音色列表中")
        if label is not None:
            voice["label"] = _s(label) or voice["label"]
        if params is not None:
            voice["params"] = normalize_voice_params(params)
        saved = self.save_profile(pid, profile)
        self._log.info("profile_voice_updated", {"id": pid, "voice_id": voice_id})
        return saved

    # ---------- 情绪档位与音频引用（v4） ----------

    def set_emotions(self, pid: str, emotions) -> dict:
        """保存存档情绪档位集合（default 恒在，非法档位静默丢弃，不抛错）。"""
        profile = self.get_profile(pid)
        profile["emotions"] = normalize_emotions(emotions)
        saved = self.save_profile(pid, profile)
        self._log.info("profile_emotions_set", {"id": pid,
                                                "emotions": saved["emotions"]})
        return saved

    def missing_report(self, pid: str, voice_id: str = "") -> dict:
        """缺失统计（读盘 + 核对文件存在性）：{"voice_id","total","missing"}。

        voice_id 缺省 = 存档当前音色；档位集合为空（无音色）时返回空 missing。
        """
        profile = self.get_profile(pid)
        vid = _s(voice_id) or _s(profile.get("current_voice"))
        return count_missing(profile, vid,
                             exists=lambda name: self.audio_exists(pid, name))

    def missing_emotions(self, pid: str, voice_id: str = "") -> dict:
        """{档位: 缺失条数}——该档无音频 / 文件不在 / text_hash 与当前文本不符都算缺。"""
        return self.missing_report(pid, voice_id)["missing"]

    def purge_emotion(self, pid: str, voice_id: str, emotion: str) -> dict:
        """清空「音色 × 情绪档」的全部音频（引用 + 文件），并把该档移出档位集合。

        default 档拒删（它是全局兜底音频，逐条重新生成即可覆盖）。
        返回 {"voice_id","emotion","files","emotions"}。
        """
        vid = _s(voice_id)
        emo = _s(emotion) or EMOTION_DEFAULT
        if emo == EMOTION_DEFAULT:
            raise ScriptStoreError("default 档是兜底音频，不可清空")
        if emo not in EMOTIONS_ALL:
            raise ScriptStoreError(f"情绪档位非法：{emo!r}")
        profile = self.get_profile(pid)
        removed = []
        for key in SECTIONS:
            for item in profile[key]:
                audios = item.get("audios") or {}
                slots = audios.get(vid)
                if not isinstance(slots, dict):
                    continue
                ref = slots.pop(emo, None)
                if isinstance(ref, dict) and _s(ref.get("file")):
                    removed.append(_s(ref.get("file")))
                if not slots:
                    audios.pop(vid, None)
        profile["emotions"] = [e for e in profile["emotions"] if e != emo]
        saved = self.save_profile(pid, profile, guard=False)
        for name in removed:
            self.delete_audio(pid, name)
        self._log.info("profile_emotion_purged", {"id": pid, "voice_id": vid,
                                                  "emotion": emo,
                                                  "removed_audio": len(removed)})
        return {"voice_id": vid, "emotion": emo, "files": len(removed),
                "emotions": list(saved["emotions"])}

    @staticmethod
    def set_audio_ref(item: dict, voice_id: str, emotion: str,
                      filename: str, text: str) -> str:
        """把「音色 × 情绪档」的新音频引用写进条目；返回被替换掉的旧文件名（无则空串）。"""
        vid = _s(voice_id)
        emo = _s(emotion) or EMOTION_DEFAULT
        name = _s(filename)
        if not vid or not name:
            raise ScriptStoreError("写入音频引用失败：音色或文件名缺失")
        if emo not in EMOTIONS_ALL:
            raise ScriptStoreError(f"情绪档位非法：{emo!r}")
        audios = item.setdefault("audios", {})
        if not isinstance(audios, dict):
            item["audios"] = audios = {}
        slots = audios.get(vid)
        if not isinstance(slots, dict):
            audios[vid] = slots = {}
        old = _s((slots.get(emo) or {}).get("file"))
        slots[emo] = {"file": name, "text_hash": text_hash(text)}
        return old

    @staticmethod
    def drop_audio_ref(item: dict, voice_id: str, emotion: str = "") -> str:
        """移除条目的某档音频引用并返回其文件名（无则空串）；该音色档位清空即删键。"""
        vid = _s(voice_id)
        emo = _s(emotion) or EMOTION_DEFAULT
        audios = item.get("audios") or {}
        slots = audios.get(vid)
        if not isinstance(slots, dict):
            return ""
        ref = slots.pop(emo, None)
        if not slots:
            audios.pop(vid, None)
        return _s(ref.get("file")) if isinstance(ref, dict) else ""

    def set_synthesizer(self, fn) -> None:
        """注入合成器 `fn(text, voice_id, params) -> wav bytes`。

        scriptstore 只管存储、不直接依赖 TTS：真正合成由装配方注入
        （`VoiceWorkshop.synthesize_wav`），测试可注入假合成器。
        """
        self._synth = fn
        self._log.info("scriptstore_synthesizer_set", {"active": fn is not None})

    def generate_audio(self, pid: str, item_id: str, voice_id: str = "",
                       emotion: str = EMOTION_DEFAULT) -> dict:
        """合成「条目 × 音色 × 情绪档」的音频并写回存档（含旧文件清理）。

        条目 id 跨 samples_ai / topics_ai 自动定位（条目 id 前缀唯一）。
        voice_id 缺省 = 存档当前音色；合成带该音色微调参数（与直播听感一致）。
        返回 {"item_id","kind","voice_id","emotion","audio","old_audio","text_hash"}。
        """
        if self._synth is None:
            raise ScriptStoreError("音频合成器未配置（未装配 MiniMax 合成通道）")
        profile = self.get_profile(pid)
        section_key, item = "", None
        for key in SECTIONS:
            item = next((x for x in profile.get(key) or []
                         if isinstance(x, dict) and x.get("id") == item_id), None)
            if item is not None:
                section_key = key
                break
        if item is None:
            raise ScriptStoreError("条目不存在（可能已被删除，请刷新列表）")
        text = _item_text(item)
        if not text:
            raise ScriptStoreError("条目文本为空，无法合成——请先填写"
                                   + ("参考回复文本" if section_key == "samples_ai"
                                      else "话题口播文本"))
        vid = _s(voice_id) or _s(profile.get("current_voice"))
        voice = next((v for v in profile.get("voices") or []
                      if v.get("voice_id") == vid), None)
        if voice is None:
            raise ScriptStoreError(f"音色 {vid or '（空）'} 不在存档音色列表中")
        emo = _s(emotion) or EMOTION_DEFAULT
        if emo not in EMOTIONS_ALL:
            raise ScriptStoreError(f"情绪档位非法：{emo!r}")
        params = dict(voice.get("params") or {})
        try:
            wav = self._synth(text, vid, params)
        except ScriptStoreError:
            raise
        except Exception as e:
            raise ScriptStoreError(f"合成失败：{e}") from e
        filename = self.save_audio(pid, wav, "audio.wav")
        old = self.set_audio_ref(item, vid, emo, filename, text)
        if emo not in list(profile.get("emotions") or []):
            # 生成了某档音频即视为登记该档：未登记档位在播放侧会被当作「现场 TTS」
            # 处理，反而每次多付一次合成延迟与费用——登记后即可命中预录。
            profile["emotions"] = normalize_emotions(
                list(profile.get("emotions") or []) + [emo])
        self.save_profile(pid, profile)
        if old and old != filename:
            self.delete_audio(pid, old)
        self._log.info("profile_audio_generated", {
            "id": pid, "item": item_id, "voice_id": vid, "emotion": emo,
            "file": filename})
        return {"item_id": item_id,
                "kind": "sample" if section_key == "samples_ai" else "topic",
                "voice_id": vid, "emotion": emo, "audio": filename,
                "old_audio": old, "text_hash": text_hash(text)}

    # ---------- 结构迁移（启动时一次） ----------

    def migrate_v1_profiles(self) -> list:
        """v1 → 最新结构一次性迁移（启动时调用）：version < 2 的存档清空样本/话题库
        并删除 audio/ 全部孤儿文件（用户决策：清空重建），共用区块保留。

        落盘时经 normalize_profile 直接写成当前结构（含 v4 的情绪档位与 audios 包装）。
        返回迁移摘要 [{"id", "cleared_topics", "cleared_samples", "removed_audio"}]。
        损坏/不可解析的存档跳过（沿用既有的 error 标记流程）。
        """
        if not self._root.is_dir():
            return []
        migrated = []
        for pdir in self._root.iterdir():
            if not pdir.is_dir() or not (pdir / PROFILE_FILE).is_file():
                continue
            try:
                raw = self._read_yaml(pdir.name)
            except ScriptStoreError:
                continue
            if not isinstance(raw, dict) or self._int_version(raw.get("version")) >= 2:
                continue
            cleared_topics = len(_as_list(raw.get("topics")))
            cleared_samples = len(_as_list(raw.get("samples")))
            removed_audio = 0
            audio_dir = pdir / AUDIO_DIR
            if audio_dir.is_dir():
                for f in audio_dir.iterdir():
                    if f.is_file():
                        try:
                            f.unlink()
                            removed_audio += 1
                        except OSError as e:
                            self._log.warning("profile_migrate_audio_remove_failed",
                                              {"id": pdir.name, "file": f.name,
                                               "error": str(e)})
            # normalize 已按「清空重建」丢弃 v1 顶层 samples/topics，此处落盘固化
            self._write_yaml(pdir.name, normalize_profile(raw))
            migrated.append({"id": pdir.name, "cleared_topics": cleared_topics,
                             "cleared_samples": cleared_samples,
                             "removed_audio": removed_audio})
            self._log.info("profile_migrated", {
                "id": pdir.name, "cleared_topics": cleared_topics,
                "cleared_samples": cleared_samples, "removed_audio": removed_audio})
        return migrated

    def migrate_v2_profiles(self, tts_voice_id: str = "",
                            voice_labels: dict = None) -> list:
        """v2 → 最新结构一次性迁移（启动时调用）：audio+voice_id → audios 多音色映射，
        从条目收集 voices，current_voice = config tts.voice_id（在 voices 内）否则
        第一个音色；template 两分区及其音频文件删除（模板方案整体退役）。

        返回迁移摘要 [{"id", "voices", "current_voice", "removed_audio"}]。
        """
        if not self._root.is_dir():
            return []
        migrated = []
        for pdir in self._root.iterdir():
            if not pdir.is_dir() or not (pdir / PROFILE_FILE).is_file():
                continue
            try:
                raw = self._read_yaml(pdir.name)
            except ScriptStoreError:
                continue
            if not isinstance(raw, dict) or self._int_version(raw.get("version")) != 2:
                continue
            data, dropped = _v2_to_v3(raw, tts_voice_id=tts_voice_id,
                                      voice_labels=voice_labels)
            removed_audio = 0
            for name in dropped:
                if not _audio_ref(name):
                    continue
                try:
                    (pdir / AUDIO_DIR / name).unlink(missing_ok=True)
                    removed_audio += 1
                except OSError as e:
                    self._log.warning("profile_migrate_audio_remove_failed",
                                      {"id": pdir.name, "file": name, "error": str(e)})
            self._write_yaml(pdir.name, normalize_profile(data))
            migrated.append({"id": pdir.name,
                             "voices": [v["voice_id"] for v in data["voices"]],
                             "current_voice": data["current_voice"],
                             "removed_audio": removed_audio})
            self._log.info("profile_migrated_v3", {
                "id": pdir.name, "voices": [v["voice_id"] for v in data["voices"]],
                "current_voice": data["current_voice"],
                "removed_audio": removed_audio})
        return migrated

    def migrate_v3_profiles(self) -> list:
        """v3 → v4 一次性迁移（启动时调用）：audios[voice_id] 的扁平
        {file, text_hash} 包成 {"default": 原值}，补 emotions 档位集合，version 置 4。

        幂等性由两点共同保证：
        1. 判定用 `_flat_to_slots`——包装后的档位值里 `file` 位于情绪档之下，
           不再命中「值内直接含 file」的扁平判定，重复运行不会二次包装；
        2. 已为 v4 结构（version ≥ 4 且无扁平 audios）的存档直接跳过，不写盘。

        迁移前把原 profile.yaml 备份为 profile.yaml.v3.bak（已存在则不覆盖，
        保留最早那份 v3 原件）。返回摘要 [{"id","audios","emotions","backup"}]。
        """
        if not self._root.is_dir():
            return []
        migrated = []
        for pdir in self._root.iterdir():
            if not pdir.is_dir() or not (pdir / PROFILE_FILE).is_file():
                continue
            try:
                raw = self._read_yaml(pdir.name)
            except ScriptStoreError:
                continue
            if not isinstance(raw, dict):
                continue
            if (self._int_version(raw.get("version")) >= PROFILE_VERSION
                    and not _has_flat_audios(raw)):
                continue
            backup = pdir / (PROFILE_FILE + BACKUP_SUFFIX_V3)
            if not backup.exists():
                try:
                    shutil.copy2(pdir / PROFILE_FILE, backup)
                except OSError as e:
                    self._log.warning("profile_backup_failed",
                                      {"id": pdir.name, "error": str(e)})
                    continue  # 备份失败不迁移：宁可留在旧结构，也不丢原始数据
            data = normalize_profile(raw)
            self._write_yaml(pdir.name, data)
            audios = sum(len(x.get("audios") or {})
                         for key in SECTIONS for x in data[key])
            migrated.append({"id": pdir.name, "audios": audios,
                             "emotions": list(data["emotions"]),
                             "backup": backup.name})
            self._log.info("profile_migrated_v4", {
                "id": pdir.name, "audios": audios, "emotions": data["emotions"],
                "backup": backup.name})
        return migrated

    @staticmethod
    def _int_version(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 1

    # ---------- 音频资产 ----------

    def _audio_name_checked(self, name: str) -> str:
        name = str(name or "").strip()
        if not name or name != Path(name).name or not _AUDIO_NAME_RE.fullmatch(name):
            raise ScriptStoreError(f"音频文件名非法：{name!r}")
        return name

    def audio_path(self, pid: str, name: str) -> Path:
        """存档内音频的绝对路径（文件名经白名单校验，防路径穿越）。"""
        return self._dir(pid) / AUDIO_DIR / self._audio_name_checked(name)

    def audio_exists(self, pid: str, name: str) -> bool:
        try:
            return self.audio_path(pid, name).is_file()
        except ScriptStoreError:
            return False

    def save_audio(self, pid: str, raw: bytes, name: str) -> str:
        """保存一段音频到存档 audio/，返回程序生成的文件名（校验规则同音色工坊）。"""
        ext = Path(str(name or "")).suffix.lower()
        if ext not in ALLOWED_EXT:
            raise ScriptStoreError(f"仅支持 mp3 / m4a / wav，当前为 {ext or '未知'}")
        if not raw:
            raise ScriptStoreError("未收到音频内容")
        if len(raw) > MAX_AUDIO_BYTES:
            raise ScriptStoreError(f"文件超过 20MB 上限（当前 {len(raw) // 1024 // 1024}MB）")
        if len(raw) < MIN_AUDIO_BYTES:
            raise ScriptStoreError("不是一个有效的音频文件（太小了），请换完整音频")
        if not _looks_like_audio(raw, ext):
            raise ScriptStoreError("文件内容不是有效的音频格式（扩展名与内容不符，或已损坏）")
        self._dir(pid)  # 先验 id 合法性再落盘
        filename = f"{secrets.token_hex(6)}{ext}"
        try:
            target = self._dir(pid) / AUDIO_DIR
            target.mkdir(parents=True, exist_ok=True)
            (target / filename).write_bytes(bytes(raw))
        except OSError as e:
            raise ScriptStoreError(f"音频保存失败：{e}") from e
        self._log.info("profile_audio_saved", {"id": pid, "file": filename,
                                               "bytes": len(raw)})
        return filename

    def delete_audio(self, pid: str, name: str) -> None:
        try:
            path = self.audio_path(pid, name)
        except ScriptStoreError:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            self._log.warning("profile_audio_delete_failed", {"error": str(e)})

    # ---------- zip 导出 / 导入 ----------

    def export_profile(self, pid: str, dest) -> str:
        """导出存档为 zip（profile.yaml + audio/*）；返回 zip 路径字符串。"""
        data = normalize_profile(self.get_profile(pid))  # 顺带校验可读；剥掉动态 stale 标注
        dest = Path(dest)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(PROFILE_FILE,
                            yaml.safe_dump({k: data[k] for k in data if k != "id"},
                                           allow_unicode=True, sort_keys=False))
                audio_dir = self._dir(pid) / AUDIO_DIR
                if audio_dir.is_dir():
                    for f in sorted(audio_dir.iterdir()):
                        if f.is_file():
                            zf.write(f, f"{AUDIO_DIR}/{f.name}")
        except OSError as e:
            raise ScriptStoreError(f"导出失败：{e}") from e
        self._log.info("profile_exported", {"id": pid, "dest": str(dest)})
        return str(dest)

    def import_profile(self, src, name: str = None) -> tuple:
        """导入 zip 为新存档（新 id，不覆盖任何现有存档）。

        v2 旧包按 v2→最新同一规则转换（template 分区音频丢弃并提示）；
        v3 旧包的扁平 audios 自动包成 default 档（静默，属无损结构升级）；
        v1 旧包沿用「清空重建」（样本/话题清空，音频不迁入）。

        返回 (profile_dict, warnings)：audio 引用缺失等可恢复问题进 warnings，
        结构性问题（非 zip / 缺 profile.yaml / yaml 损坏）抛 ScriptStoreError。
        """
        warnings: list[str] = []
        try:
            zf = zipfile.ZipFile(src)
        except (zipfile.BadZipFile, OSError) as e:
            raise ScriptStoreError(f"导入失败：不是有效的 zip 文件（{e}）") from e
        with zf:
            names = zf.namelist()
            if PROFILE_FILE not in names:
                raise ScriptStoreError("导入失败：zip 里没有 profile.yaml")
            try:
                data = yaml.safe_load(zf.read(PROFILE_FILE).decode("utf-8"))
            except (yaml.YAMLError, UnicodeDecodeError) as e:
                raise ScriptStoreError(f"导入失败：profile.yaml 解析失败（{e}）") from e
            # 只收 audio/ 直下的合法音频名——嵌套目录/路径穿越/超尺寸一律跳过
            audio_entries: dict[str, bytes] = {}
            for item in names:
                if not item.startswith(f"{AUDIO_DIR}/") or item.endswith("/"):
                    continue
                base = Path(item).name
                if f"{AUDIO_DIR}/{base}" != item or not _AUDIO_NAME_RE.fullmatch(base):
                    warnings.append(f"跳过非法音频条目：{item}")
                    continue
                raw = zf.read(item)
                ext = Path(base).suffix.lower()
                if ext not in ALLOWED_EXT or len(raw) > MAX_AUDIO_BYTES \
                        or not _looks_like_audio(raw, ext):
                    warnings.append(f"跳过无效音频：{base}")
                    continue
                audio_entries[base] = raw

        version = self._int_version((data or {}).get("version"))
        if version == 2:
            data, dropped = _v2_to_v3(data)
            for name in dropped:
                warnings.append(f"模板分区音频已丢弃：{name}")
        elif version < 2:
            warnings.append("旧版存档（v1）：样本与话题库已按「清空重建」规则清空")

        profile = normalize_profile(data)
        if _s(name):
            profile["name"] = _s(name)
        # 丢弃 zip 里实际不存在的音频引用（条目保留，仅该音色该档音频落空）
        referenced: set[str] = set()
        for section in SECTIONS:
            for entry in profile[section]:
                audios = entry.get("audios") or {}
                for vid in list(audios.keys()):
                    slots = audios.get(vid)
                    if not isinstance(slots, dict):
                        del audios[vid]
                        continue
                    for emo in list(slots.keys()):
                        ref = str((slots.get(emo) or {}).get("file") or "")
                        if ref and ref not in audio_entries:
                            warnings.append(f"音频缺失已落空：{ref}")
                            del slots[emo]
                        elif ref:
                            referenced.add(ref)
                    if not slots:
                        del audios[vid]

        pid = _new_id("p")
        self._write_yaml(pid, profile)
        # 只落盘被条目引用的音频：未引用的一律跳过（不静默堆积孤儿）
        for base, raw in audio_entries.items():
            if base not in referenced:
                warnings.append(f"音频未被引用，跳过：{base}")
                continue
            try:
                (self._dir(pid) / AUDIO_DIR / base).write_bytes(raw)
            except OSError as e:
                warnings.append(f"音频落盘失败：{base}（{e}）")
        self._log.info("profile_imported", {"id": pid, "name": profile["name"],
                                            "audio": len(audio_entries),
                                            "warnings": len(warnings)})
        return self.get_profile(pid), warnings


SEED_ZIP = BUNDLE_DIR / "assets" / "seed_profile.zip"


def find_seed_zip() -> Path | None:
    """找首跑种子 zip：先随包内置，再 exe / 项目根旁的 `assets/`（发布包外置）。

    为什么要外置：v4 种子（3 情绪档）已到 ~69 MB，**塞进 onefile exe 会让 exe 膨胀
    到 88 MB 并把启动解包时间拖长**；发布包把它放在 exe 旁的 `assets/` 里，首跑照样
    自动落地。`SEED_ZIP` 仍是包内规范路径（也便于测试 monkeypatch）。
    """
    for candidate in (SEED_ZIP, EXE_DIR / "assets" / "seed_profile.zip"):
        if candidate.is_file():
            return candidate
    return None


def seed_profiles_if_empty(store: "ScriptStore") -> str:
    """首跑种子（双击版）：profiles 为空且找得到种子 zip 时，解出为新存档。

    返回新存档 id；已有存档 / 找不到种子（源码运行且未外置）/ 种子损坏时返回 ""。
    激活指针由调用方的运行时兜底设置（ensure_default 会选中这个唯一存档）。
    """
    try:
        if store.list_profiles():
            return ""
    except OSError:
        return ""
    seed = find_seed_zip()
    if seed is None:
        return ""
    try:
        profile, _warnings = store.import_profile(str(seed))
    except ScriptStoreError as e:
        store._log.warning("profile_seed_failed", {"error": str(e)})
        return ""
    store._log.info("profile_seeded", {"id": profile["id"], "name": profile["name"],
                                       "seed": str(seed)})
    return profile["id"]
