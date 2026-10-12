"""配置加载与校验（PROMPTS 步骤 0.1）。

密钥双文件约定（DESIGN §4.9 / PRD FR-8）：
- LLM 密钥在 config.yaml 的 llm.api_key（该文件不入 git）
- MiniMax 密钥与端点只存 voice_clone/voice_clone_data/config.json（向导维护）
"""
import json
import os
import re
import sys
from pathlib import Path

import yaml

from hub.paths import EXE_DIR
from hub.safety import load_words

# 冻结（PyInstaller）时锚定 exe 旁的 voice_clone/，源码运行为项目根
PROJECT_ROOT = EXE_DIR
MINIMAX_SECRET_PATH = PROJECT_ROOT / "voice_clone" / "voice_clone_data" / "config.json"

_CONFIG = None


class ConfigError(Exception):
    """配置缺失或非法。message 为完整问题清单（多行）。"""


def _load_minimax_secret(path: Path) -> dict:
    """读取 voice_clone_data/config.json，返回 {'api_key': ..., 'endpoint': ...}。

    文件缺失或字段为空时对应值为空字符串，由 validate 统一报清单。
    """
    empty = {"api_key": "", "endpoint": ""}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        empty["_missing_file"] = str(path)
        return empty
    except (json.JSONDecodeError, OSError) as e:
        empty["_parse_error"] = f"{type(e).__name__}: {e}"
        return empty
    if not isinstance(data, dict):
        empty["_parse_error"] = "config.json 内容不是 JSON 对象"
        return empty
    return {
        "api_key": str(data.get("api_key") or ""),
        "endpoint": str(data.get("endpoint") or ""),
    }


def _get(cfg: dict, dotted: str, default=None):
    node = cfg
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


# MiniMax 语气情绪白名单（speech-2.8-hd 支持；留空 = 模型按文案自动推断）
TTS_EMOTIONS = ("happy", "sad", "angry", "fearful",
                "disgusted", "surprised", "calm", "fluent")


def _check_number(errors: list, cfg: dict, dotted: str, lo, hi, kind="数字"):
    """类型与范围校验，错误信息指名道姓。"""
    value = _get(cfg, dotted)
    if value is None:
        return  # 缺失已由必填项检查覆盖；选填数值缺失不重复报
    if kind == "整数" and not isinstance(value, int) or kind == "数字" and not isinstance(value, (int, float)) or isinstance(value, bool):
        errors.append(f"[类型] {dotted} 必须为{kind}，当前值: {value!r}")
        return
    if lo is not None and value < lo or hi is not None and value > hi:
        range_txt = f"{lo}~{hi}"
        errors.append(f"[范围] {dotted} 必须在 {range_txt} 之间，当前值: {value!r}")


def validate_config(cfg: dict, base_dir: Path) -> list:
    """返回全部问题清单（空列表即通过）。base_dir 用于解析相对路径。"""
    errors: list = []

    # LLM 三项可选（用户需求：无需配置 LLM 密钥也可打开）：
    # 空/PLACEHOLDER 由 llm_configured 判定未配置，弹幕回复停用但应用可正常启动，
    # 控制台「配置 LLM」可填写并热生效

    # TTS / MiniMax 三项可选（用户需求：无需 MiniMax Key 也可打开）：
    # 未配置时启动不失败，播放侧退回打印模式；控制台「音色工坊」可配置并热生效
    # （tts.voice_id 留空时用模板默认值兜底，避免 KeyError）

    # 重放文件存在性
    replay_file = str(_get(cfg, "app.replay_file", "") or "").strip()
    if replay_file:
        replay_path = Path(replay_file)
        if not replay_path.is_absolute():
            replay_path = base_dir / replay_path
        if not replay_path.exists():
            errors.append(f"[缺失] app.replay_file 指定的文件不存在: {replay_path}")

    # 敏感词库（步骤 4.1）：文件必须存在且有有效词条
    words_file = str(_get(cfg, "safety.block_words_file", "blockwords.txt") or "").strip()
    words_path = Path(words_file) if words_file else (base_dir / "blockwords.txt")
    if not words_path.is_absolute():
        words_path = base_dir / words_path
    if not words_path.exists():
        errors.append(f"[缺失] safety.block_words_file 指定的词库不存在: {words_path}")
    else:
        try:
            words = load_words(words_path)
        except OSError as e:
            errors.append(f"[错误] 词库文件读取失败: {e}")
            words = []
        if not words:
            errors.append(f"[缺失] 词库无有效词条（一行一词，# 开头为注释）: {words_path}")

    # 数值参数（PRD 各 FR 参数表的可调范围）
    _check_number(errors, cfg, "events.chat.merge_window_s", 1, 10, "数字")
    _check_number(errors, cfg, "events.chat.stale_drop_s", 10, 60, "数字")
    _check_number(errors, cfg, "events.chat.stale_drop_p1_s", 10, 300, "数字")
    _check_number(errors, cfg, "events.chat.stale_drop_p2_s", 10, 600, "数字")
    _check_number(errors, cfg, "events.chat.ignore_noise_p", 0, 1, "数字")
    _check_number(errors, cfg, "events.gift.combo_timeout_s", 1, 60, "数字")
    _check_number(errors, cfg, "events.follow.cooldown_s", 5, 600, "数字")
    _check_number(errors, cfg, "events.follow.probability", 0, 1, "数字")
    _check_number(errors, cfg, "events.fansclub.cooldown_s", 5, 600, "数字")
    _check_number(errors, cfg, "events.fansclub.probability", 0, 1, "数字")
    _check_number(errors, cfg, "llm.temperature", 0, 2, "数字")
    _check_number(errors, cfg, "llm.max_tokens", 1, 4096, "整数")
    _check_number(errors, cfg, "llm.timeout_seconds", 1, 60, "数字")
    _check_number(errors, cfg, "persona.max_reply_chars", 20, 60, "整数")
    _check_number(errors, cfg, "audio.sample_rate", 8000, 96000, "整数")
    _check_number(errors, cfg, "audio.channels", 1, 2, "整数")
    _check_number(errors, cfg, "audio.gap_ms", 0, 5000, "整数")
    # TTS 精细化参数（音色工坊「参数微调」写入；与 MiniMax 官方取值域一致）
    _check_number(errors, cfg, "tts.speed", 0.5, 2.0, "数字")
    _check_number(errors, cfg, "tts.vol", 0, 10, "数字")
    _check_number(errors, cfg, "tts.pitch", -12, 12, "数字")
    emotion = _get(cfg, "tts.emotion", "")
    if emotion and emotion not in TTS_EMOTIONS:
        errors.append(f"[范围] tts.emotion 必须是 {sorted(TTS_EMOTIONS)} 之一或留空，当前值: {emotion!r}")
    _check_number(errors, cfg, "schedule.interval_min", 1, 120, "数字")
    _check_number(errors, cfg, "watchdog.fail_threshold", 1, 1000, "整数")

    # 智能体段（DESIGN §12.1）：只在显式给出时校验——缺项由 agent_settings 取出厂默认。
    # 运行时宽容（_as_bool/_clamp_int），此处严格：写错了要让人看见。
    _check_number(errors, cfg, "agent.retrieve.top_k", 1, 20, "整数")
    _check_number(errors, cfg, "agent.memory.max_events", 1, 500, "整数")
    _check_number(errors, cfg, "agent.memory.max_spoken", 1, 200, "整数")
    _check_number(errors, cfg, "agent.memory.max_notes", 0, 100, "整数")
    agent_layer = _get(cfg, "agent.retrieve.layer")
    if agent_layer is not None and str(agent_layer).strip().lower() not in AGENT_LAYERS:
        errors.append(f"[范围] agent.retrieve.layer 必须是 {'/'.join(AGENT_LAYERS)} 之一，"
                      f"当前值: {agent_layer!r}")
    for dotted in ("agent.enabled", "agent.feedback.enabled",
                   "agent.emotion.enabled", "agent.emotion.onsite_fallback",
                   "agent.decision_log"):
        value = _get(cfg, dotted)
        if value is not None and not isinstance(value, bool):
            errors.append(f"[类型] {dotted} 必须为布尔值（true/false），当前值: {value!r}")

    # 可视化控制台（用户需求）：显式 enabled 才监听，listen 必须是 host:port
    webui = (cfg.get("webui", {}) or {})
    if webui.get("enabled", False):
        listen = str(webui.get("listen", "") or "").strip()
        host, _, port_s = listen.rpartition(":")
        try:
            port = int(port_s)
        except ValueError:
            port = -1
        if not host or not 0 <= port < 65536:
            errors.append(f"[格式] webui.listen 必须为 host:port（如 127.0.0.1:8766），当前值: {listen or '<空>'}")

    return errors


def load_config(path: str = "config.yaml") -> dict:
    """加载 config.yaml，合并 MiniMax 密钥并校验。失败抛 ConfigError。"""
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise ConfigError(f"[缺失] 配置文件不存在: {cfg_path.resolve()}")

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"[错误] config.yaml 解析失败: {e}")
    if not isinstance(cfg, dict):
        raise ConfigError("[错误] config.yaml 顶层必须是键值映射")

    # MiniMax 密钥合并进运行时配置（DESIGN §4.9：单一数据源）
    if str(cfg.get("tts", {}).get("api_key", "") or "").strip():
        print("警告: config.yaml 的 tts 组出现 api_key——MiniMax 密钥统一由音色向导维护于"
              " voice_clone/voice_clone_data/config.json，此处填写不会生效", file=sys.stderr)
    mm = _load_minimax_secret(MINIMAX_SECRET_PATH)
    cfg["_minimax"] = mm
    tts = cfg.setdefault("tts", {})
    if isinstance(tts, dict):
        tts["api_key"] = mm.get("api_key", "")
        tts["endpoint"] = mm.get("endpoint", "")

    # 词库路径解析为绝对路径写回（运行时读它，不再依赖 CWD）
    safety = cfg.setdefault("safety", {})
    if isinstance(safety, dict):
        wf = str(safety.get("block_words_file", "") or "").strip() or "blockwords.txt"
        wf_path = Path(wf)
        if not wf_path.is_absolute():
            wf_path = cfg_path.resolve().parent / wf_path
        safety["block_words_file"] = str(wf_path)

    errors = validate_config(cfg, cfg_path.resolve().parent)
    if errors:
        raise ConfigError("\n".join(errors))
    # 供控制台「配置 LLM」写回定位（_ 前缀私有键，与 _minimax 同一模式）
    cfg["_config_path"] = str(cfg_path.resolve())
    return cfg


def get_config(path: str = None) -> dict:
    """模块级单例；首次调用时加载（可用 ZAIZAISHU_CONFIG 环境变量指定路径）。"""
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config(path or os.environ.get("ZAIZAISHU_CONFIG", "config.yaml"))
    return _CONFIG


def llm_configured(cfg: dict) -> bool:
    """LLM 是否已配置可用：base_url/api_key/model 三项均非空且非 PLACEHOLDER 占位。

    未配置不是错误——应用照常启动，仅弹幕文字回复停用（控制台可填写后热生效）。
    """
    llm = (cfg or {}).get("llm", {}) or {}
    for key in ("base_url", "api_key", "model"):
        value = str(llm.get(key, "") or "").strip()
        if not value or value.upper().startswith("PLACEHOLDER"):
            return False
    return True


def minimax_configured(cfg: dict) -> bool:
    """MiniMax TTS 是否已配置可用：api_key 非空（load_config 已合并 _minimax）。

    未配置不是错误——应用照常启动，播放侧退回打印模式；控制台「音色工坊」
    保存 Key 后热生效（main._reconfigure_tts 重建 TTS 客户端）。
    """
    tts = (cfg or {}).get("tts", {}) or {}
    return bool(str(tts.get("api_key", "") or "").strip())


# ---------- 智能体段（v4 / M3，DESIGN §12.1） ----------

# 检索层：l1 仅素材直注 / l2 追加关键词召回 / l3 语义向量（预留，未实现）
AGENT_LAYERS = ("l1", "l2", "l3")
# 可写回的叶子键白名单：update_agent_config 只认这些，避免把任意键塞进配置文件
AGENT_VALUE_KEYS = ("enabled", "layer", "top_k", "max_events", "max_spoken",
                    "max_notes", "decision_log")
_TRUE_WORDS = ("1", "true", "yes", "on", "y", "t")
_FALSE_WORDS = ("0", "false", "no", "off", "n", "f", "")


def _as_bool(value, default: bool) -> bool:
    """宽容布尔：认 YAML 原生 bool，也认手写配置里的 true/false/on/off/1/0。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return default


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    """整数夹取：非整数/缺失取默认，越界夹到边界（不抛异常）。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def agent_settings(cfg: dict) -> dict:
    """智能体 `agent` 段归一化（DESIGN §12.1）：缺项取出厂默认，越界夹取、非法回落。

    这里**刻意宽容**：开关未开启时这些参数不影响任何行为，为它们写错值而拒绝
    启动是过重的惩罚（严格报错走 `validate_config`，那是给人看的校验通道）。
    返回结构稳定——main 装配、webui 状态广播、M4 的 brain/retriever 都按它取键。
    """
    a = (cfg or {}).get("agent", {}) or {}
    layer = str(_get(a, "retrieve.layer", "") or "").strip().lower()
    return {
        "enabled": _as_bool(a.get("enabled"), False),
        "retrieve": {
            "layer": layer if layer in AGENT_LAYERS else "l2",
            "top_k": _clamp_int(_get(a, "retrieve.top_k"), 5, 1, 20),
        },
        "memory": {
            "max_events": _clamp_int(_get(a, "memory.max_events"), 50, 1, 500),
            "max_spoken": _clamp_int(_get(a, "memory.max_spoken"), 20, 1, 200),
            "max_notes": _clamp_int(_get(a, "memory.max_notes"), 10, 0, 100),
        },
        "feedback": {"enabled": _as_bool(_get(a, "feedback.enabled"), True)},
        "emotion": {
            "enabled": _as_bool(_get(a, "emotion.enabled"), True),
            "onsite_fallback": _as_bool(_get(a, "emotion.onsite_fallback"), True),
        },
        "decision_log": _as_bool(a.get("decision_log"), True),
    }


def agent_available(cfg: dict) -> bool:
    """智能体开关能否开启：硬前提是 LLM 已配置（每个事件都要调一次 LLM 做决策）。

    未配置时应用照常启动、弹幕/礼物/轮播链路完全照旧，只是控制台的总开关
    置灰并给出明确提示（DESIGN §5.3 第 4 条 / V3.3）。
    """
    return llm_configured(cfg)


def agent_status(cfg: dict) -> dict:
    """控制台「智能体」卡片所需的完整状态（webui 直接广播，前端不再拼装）。"""
    settings = agent_settings(cfg)
    return {
        "enabled": settings["enabled"],
        # available = 允许切换；reason 是置灰时给用户看的原因（而不是一句"不可用"）
        "available": agent_available(cfg),
        "llm_configured": llm_configured(cfg),
        "settings": settings,
    }


# llm 段内可直接改写的三项（键 → 行内正则，缩进按模板 2 空格）
_LLM_LINE_RE = re.compile(r"^(\s+)(base_url|api_key|model)\s*:\s*(.*)$")


def _yaml_quote(value: str) -> str:
    """值统一双引号输出（json 转义是 YAML 双引号风格的子集，含特殊字符也安全）。"""
    return json.dumps(str(value), ensure_ascii=False)


def _extract_line_comment(raw: str) -> str:
    """提取 YAML 行尾注释（不含前导空白；无注释返回 ""）。

    双引号标量内的 # 属于值本身，不能当注释起点——本模块写出的值一律双引号，
    而设备名等值可能含 #（如蓝牙 Hands-Free 设备的 mangled 名
    "…bthhfenum.sys,#2;%1 Hands-Free%0\\r\\n;(X)"）；naive partition("#")
    会把旧值尾巴当作注释粘进新行，config.yaml 腐蚀且逐次累积（设备行事故）。
    """
    m = re.match(r'^\s*"(?:[^"\\]|\\.)*"\s*(#.*)?$', raw)
    if m:  # 双引号值：引号闭合后的 # 才是注释
        return m.group(1) or ""
    head, sep, tail = raw.partition(" #")  # 裸值：YAML 规定注释的 # 前必有空白
    return f"#{tail}" if sep else ""


def update_llm_config(path, base_url: str = None, api_key: str = None,
                      model: str = None) -> None:
    """把 LLM 配置写回 config.yaml（行级替换，保留全部注释与其他配置）。

    None 的字段保持文件原值不动。config.yaml 的中文注释是主播调参的
    唯一指引（曾发生外部工具整文件还原事故），故不用 yaml.dump 整体重写。
    """
    updates = {k: v for k, v in (("base_url", base_url), ("api_key", api_key),
                                 ("model", model)) if v is not None}
    if not updates:
        return
    path = Path(path)
    lines = path.read_text(encoding="utf-8").split("\n")

    llm_idx = None
    for i, line in enumerate(lines):
        if line.rstrip() == "llm:":
            llm_idx = i
            break

    if llm_idx is None:
        # 文件没有 llm 段（极端情况）：追加到末尾
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("llm:")
        for key, value in updates.items():
            lines.append(f"  {key}: {_yaml_quote(value)}")
    else:
        end = len(lines)
        for i in range(llm_idx + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped and not lines[i][:1].isspace() and not stripped.startswith("#"):
                end = i
                break
        section = lines[llm_idx + 1:end]
        replaced = set()
        for i, line in enumerate(section):
            m = _LLM_LINE_RE.match(line)
            if m and m.group(2) in updates:
                replaced.add(m.group(2))
                # 行尾注释（# 起）原样保留，只换值
                comment = _extract_line_comment(m.group(3))
                suffix = f"  {comment}" if comment else ""
                section[i] = f"{m.group(1)}{m.group(2)}: {_yaml_quote(updates[m.group(2)])}{suffix}"
        # 段内缺失的键：插到 llm: 行之后
        missing = [f"  {key}: {_yaml_quote(updates[key])}"
                   for key in ("base_url", "api_key", "model")
                   if key in updates and key not in replaced]
        lines[llm_idx + 1:end] = missing + section

    path.write_text("\n".join(lines), encoding="utf-8")


# ---------- agent 段行级写回（保留注释，与 update_llm_config 同策略） ----------

_BARE_SCALAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
_YAML_RESERVED = ("true", "false", "yes", "no", "on", "off", "null", "none", "y", "n")


def _yaml_scalar(value) -> str:
    """标量 → YAML 字面量。布尔写小写 true/false（YAML 1.1 认 True/False，
    但小写更直观，且与出厂模板一致）。

    字符串只在必要时加双引号：枚举值（layer: l2）保持裸写，与 config.yaml
    模板一致、免得每次开关都产生引号 diff；含特殊字符或会被 YAML 当布尔/
    空值的串才走 _yaml_quote 兜底。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if _BARE_SCALAR_RE.match(text) and text.lower() not in _YAML_RESERVED:
        return text
    return _yaml_quote(text)


def _block_bounds(lines: list, lo: int, hi: int, indent: int, key: str):
    """在 [lo,hi) 内定位 **indent 缩进** 的 `key:` 行及其块结束行，返回 (i, j)。

    缩进必须精确相等：`agent.enabled`（2 空格）与 `agent.emotion.enabled`
    （4 空格）同名不同层，靠缩进区分才不会改错行。
    块结束 = 之后第一个「非空且缩进 ≤ indent」的行。
    """
    pad = " " * indent
    for i in range(lo, hi):
        line = lines[i]
        if not line.startswith(pad):
            continue
        body = line[len(pad):]
        if body[:1] in (" ", "\t"):
            continue                                # 更深一层，不是本层键
        name, sep, _ = body.partition(":")
        if not sep or name.strip() != key:
            continue
        j = i + 1
        while j < hi:
            s = lines[j]
            if not s.strip():
                j += 1
                continue
            if len(s) - len(s.lstrip()) <= indent:  # 同层兄弟键或上一级 → 块到头
                break
            j += 1
        return i, j
    return None, None


def _agent_section_end(lines: list, sec_idx: int) -> int:
    """agent 段行区间末尾：之后第一个非缩进的非空行（注释行归上一段）。"""
    for i in range(sec_idx + 1, len(lines)):
        if lines[i].strip() and not lines[i][:1].isspace():
            return i
    return len(lines)


def _set_agent_leaf(lines: list, sec_idx: int, end: int, parts: list, value) -> None:
    """在 agent 段内写一个叶子键（parts 为从 agent 往下的点分路径）。

    同层已有 → 换值并保留行尾注释；父级缺失 → 在段末补齐整条链；
    叶子缺失 → 插在父块内最后一个「本层键」之后（不与嵌套块交错）。
    """
    leaf, parents = parts[-1], parts[:-1]
    lo, hi = sec_idx + 1, end
    for depth, key in enumerate(parents):
        i, j = _block_bounds(lines, lo, hi, 2 + 2 * depth, key)
        if i is None:
            block = [f"{' ' * (2 + 2 * d)}{parents[d]}:"
                     for d in range(depth, len(parents))]
            block.append(f"{' ' * (2 + 2 * len(parents))}{leaf}: {_yaml_scalar(value)}")
            lines[end:end] = block
            return
        lo, hi = i + 1, j

    pad = " " * (2 + 2 * len(parents))
    insert_at = lo
    for i in range(lo, hi):
        line = lines[i]
        if not line.startswith(pad):
            continue
        body = line[len(pad):]
        if body[:1] in (" ", "\t"):
            continue                                # 嵌套块内的行不算同层键
        name, sep, rest = body.partition(":")
        if not sep:
            continue
        if name.strip() == leaf:
            comment = _extract_line_comment(rest)
            suffix = f"  {comment}" if comment else ""
            lines[i] = f"{pad}{leaf}: {_yaml_scalar(value)}{suffix}"
            return
        insert_at = i + 1                           # 记住最后一个同层键的位置
    lines[insert_at:insert_at] = [f"{pad}{leaf}: {_yaml_scalar(value)}"]


def update_agent_config(path, updates: dict) -> None:
    """把 agent 段配置写回 config.yaml（行级替换，保留全部注释与其他配置）。

    `updates` 的键是相对 agent 段的点分路径，值可为 bool / int / str：
        {"enabled": True}                    → `  enabled: true`
        {"retrieve.layer": "l1"}             → `    layer: l1`（`retrieve:` 不存在则补齐）
    值为 None 的键跳过；不在 AGENT_VALUE_KEYS 白名单内的键**丢弃**（防止把
    任意键塞进用户配置文件）。文件没有 agent 段时追加一段最小可用的。
    """
    wanted = {k: v for k, v in (updates or {}).items() if v is not None}
    wanted = {k: v for k, v in wanted.items()
              if k.split(".")[-1] in AGENT_VALUE_KEYS}
    if not wanted:
        return
    path = Path(path)
    lines = path.read_text(encoding="utf-8").split("\n")

    sec_idx = None
    for i, line in enumerate(lines):
        if line.rstrip() == "agent:":
            sec_idx = i
            break
    if sec_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("agent:")
        lines.append("  enabled: false")
        sec_idx = len(lines) - 2                    # 指向 "agent:" 行本身，不是种子键行

    for dotted, value in wanted.items():
        parts = [p for p in str(dotted).split(".") if p]
        if not parts:
            continue
        # 每个键单独重算区间：前一条可能刚插入过行，行号已变。
        # 段尾空行（agent 段与下一段之间的分隔）不参与插入点，否则新键会
        # 落进空行之后、与所属父块脱开。
        end = _agent_section_end(lines, sec_idx)
        while end > sec_idx + 1 and not lines[end - 1].strip():
            end -= 1
        _set_agent_leaf(lines, sec_idx, end, parts, value)

    path.write_text("\n".join(lines), encoding="utf-8")


def mask_secret(value: str) -> str:
    """密钥打码：显示前 4 位。"""
    value = str(value or "")
    if not value.strip():
        return "<空>"
    return value[:4] + "****"


def script_settings(cfg: dict) -> dict:
    """话术管理指针（v3.1）：scheme 仅认 ai/template（非法值回落 ai）。

    active_profile 为存档 id（profiles/<id>/）；留空表示尚未选定，
    启动时由 main 经 ScriptStore.ensure_default 兜底并写回。
    """
    sc = (cfg or {}).get("script", {}) or {}
    scheme = str(sc.get("scheme", "") or "").strip()
    if scheme not in ("ai", "template"):
        scheme = "ai"
    return {"scheme": scheme,
            "active_profile": str(sc.get("active_profile", "") or "").strip()}


def update_script_config(path, scheme: str = None, active_profile: str = None) -> None:
    """把 script.scheme / script.active_profile 写回 config.yaml（行级替换保注释）。

    None 的字段保持文件原值不动；文件没有 script 段时追加到末尾
    （与 update_llm_config 同一策略：config.yaml 的中文注释是主播调参唯一指引）。
    """
    updates = {k: v for k, v in (("scheme", scheme), ("active_profile", active_profile))
               if v is not None}
    if not updates:
        return
    path = Path(path)
    lines = path.read_text(encoding="utf-8").split("\n")
    key_re = re.compile(r"^(\s+)(scheme|active_profile)\s*:\s*(.*)$")

    sec_idx = None
    for i, line in enumerate(lines):
        if line.rstrip() == "script:":
            sec_idx = i
            break

    if sec_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("script:")
        for key in ("scheme", "active_profile"):
            if key in updates:
                lines.append(f"  {key}: {_yaml_quote(updates[key])}")
    else:
        end = len(lines)
        for i in range(sec_idx + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped and not lines[i][:1].isspace() and not stripped.startswith("#"):
                end = i
                break
        replaced = set()
        for i in range(sec_idx + 1, end):
            m = key_re.match(lines[i])
            if m and m.group(2) in updates:
                replaced.add(m.group(2))
                comment = _extract_line_comment(m.group(3))
                suffix = f"  {comment}" if comment else ""
                lines[i] = f"{m.group(1)}{m.group(2)}: {_yaml_quote(updates[m.group(2)])}{suffix}"
        missing = [f"  {key}: {_yaml_quote(updates[key])}"
                   for key in ("scheme", "active_profile")
                   if key in updates and key not in replaced]
        lines[sec_idx + 1:sec_idx + 1] = missing

    path.write_text("\n".join(lines), encoding="utf-8")


def update_schedule_config(path, interval_min=None, enabled=None) -> None:
    """把 schedule.interval_min / schedule.enabled 写回 config.yaml（行级替换保注释）。

    与 update_safety_config 同一策略：只改目标键所在行，行尾中文注释原样保留
    （config.yaml 的注释是主播调参的唯一指引，不能被写配置冲掉）。

    `interval_min` 越界（与 `validate_config` 同口径 1~120）**当场 ValueError**：
    宁可这条指令失败，也不能写一个下次启动校验失败的配置进去。

    注意这是"引导值"而不是运行时权威——有存档时真正生效的是
    `profiles/<pid>/profile.yaml` 的 `schedule.interval_s`（见 DESIGN §7.2① 与
    M6 实测记录）。`hub.agent.session` 两处一起写，就是为了不让两者打架。
    """
    updates = {}
    if interval_min is not None:
        try:
            value = float(interval_min)
        except (TypeError, ValueError):
            raise ValueError(f"schedule.interval_min 要是数字：{interval_min!r}") from None
        if value < 1 or value > 120:
            raise ValueError(f"schedule.interval_min 需在 1 ~ 120（收到 {value}）")
        value = int(value) if float(value).is_integer() else round(value, 2)
        updates["interval_min"] = str(value)
    if enabled is not None:
        updates["enabled"] = "true" if enabled else "false"
    if not updates:
        return
    path = Path(path)
    lines = path.read_text(encoding="utf-8").split("\n")
    key_re = re.compile(r"^(\s+)(interval_min|enabled)\s*:\s*(.*)$")

    sec_idx = None
    for i, line in enumerate(lines):
        if line.rstrip() == "schedule:":
            sec_idx = i
            break

    if sec_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("schedule:")
        for key in ("enabled", "interval_min"):
            if key in updates:
                lines.append(f"  {key}: {updates[key]}")
    else:
        end = len(lines)
        for i in range(sec_idx + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped and not lines[i][:1].isspace() and not stripped.startswith("#"):
                end = i
                break
        replaced = set()
        # 只认"缩进 2 空格"的那一层：schedule.pool 之下还有同名的缩进行不能误伤
        for i in range(sec_idx + 1, end):
            m = key_re.match(lines[i])
            if m and len(m.group(1)) == 2 and m.group(2) in updates:
                replaced.add(m.group(2))
                comment = _extract_line_comment(m.group(3))
                suffix = f"  {comment}" if comment else ""
                lines[i] = f"{m.group(1)}{m.group(2)}: {updates[m.group(2)]}{suffix}"
        missing = [f"  {key}: {updates[key]}"
                   for key in ("enabled", "interval_min")
                   if key in updates and key not in replaced]
        lines[sec_idx + 1:sec_idx + 1] = missing

    path.write_text("\n".join(lines), encoding="utf-8")


def update_safety_config(path, fallback_reply: str = None, fallback_enabled=None) -> None:
    """把 safety.fallback_reply / safety.fallback_enabled 写回 config.yaml（行级替换保注释）。

    兜底文案可改、兜底口播可开/关；None 的字段保持原值（开关显式传 True/False）。
    与 update_llm_config 同一保注释策略（config.yaml 中文注释是主播调参唯一指引）。
    """
    updates = {}
    if fallback_reply is not None:
        updates["fallback_reply"] = _yaml_quote(str(fallback_reply))
    if fallback_enabled is not None:
        updates["fallback_enabled"] = "true" if fallback_enabled else "false"
    if not updates:
        return
    path = Path(path)
    lines = path.read_text(encoding="utf-8").split("\n")
    key_re = re.compile(r"^(\s+)(fallback_reply|fallback_enabled)\s*:\s*(.*)$")

    sec_idx = None
    for i, line in enumerate(lines):
        if line.rstrip() == "safety:":
            sec_idx = i
            break

    if sec_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("safety:")
        for key in ("fallback_reply", "fallback_enabled"):
            if key in updates:
                lines.append(f"  {key}: {updates[key]}")
    else:
        end = len(lines)
        for i in range(sec_idx + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped and not lines[i][:1].isspace() and not stripped.startswith("#"):
                end = i
                break
        replaced = set()
        for i in range(sec_idx + 1, end):
            m = key_re.match(lines[i])
            if m and m.group(2) in updates:
                replaced.add(m.group(2))
                comment = _extract_line_comment(m.group(3))
                suffix = f"  {comment}" if comment else ""
                lines[i] = f"{m.group(1)}{m.group(2)}: {updates[m.group(2)]}{suffix}"
        missing = [f"  {key}: {updates[key]}"
                   for key in ("fallback_reply", "fallback_enabled")
                   if key in updates and key not in replaced]
        lines[sec_idx + 1:sec_idx + 1] = missing

    path.write_text("\n".join(lines), encoding="utf-8")


def update_tts_voice(path, voice_id: str) -> None:
    """把 tts.voice_id 写回 config.yaml（行级替换，保留全部注释与其他配置）。

    音色工坊「设为直播音色」用：与 update_llm_config 同一保注释策略
    （config.yaml 中文注释是主播调参唯一指引）。
    """
    value = str(voice_id or "").strip()
    if not value:
        return
    path = Path(path)
    lines = path.read_text(encoding="utf-8").split("\n")
    key_re = re.compile(r"^(\s+)voice_id\s*:\s*(.*)$")

    sec_idx = None
    for i, line in enumerate(lines):
        if line.rstrip() == "tts:":
            sec_idx = i
            break

    if sec_idx is None:
        # 文件没有 tts 段（极端情况）：追加到末尾
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("tts:")
        lines.append(f"  voice_id: {_yaml_quote(value)}")
    else:
        end = len(lines)
        for i in range(sec_idx + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped and not lines[i][:1].isspace() and not stripped.startswith("#"):
                end = i
                break
        replaced = False
        for i in range(sec_idx + 1, end):
            m = key_re.match(lines[i])
            if m:
                comment = _extract_line_comment(m.group(2))
                suffix = f"  {comment}" if comment else ""
                lines[i] = f"{m.group(1)}voice_id: {_yaml_quote(value)}{suffix}"
                replaced = True
                break
        if not replaced:
            lines.insert(sec_idx + 1, f"  voice_id: {_yaml_quote(value)}")

    path.write_text("\n".join(lines), encoding="utf-8")


def update_tts_params(path, updates: dict) -> None:
    """把 tts 段的 speed/vol/pitch/emotion 写回 config.yaml（行级替换，保留注释）。

    音色工坊「参数微调」保存直播音色参数时用；与 update_tts_voice 同一策略。
    updates 只接受这四个键，其余忽略；emotion 为字符串（加引号），其余按数字原样写。
    """
    allowed = {"speed", "vol", "pitch", "emotion"}
    updates = {k: v for k, v in (updates or {}).items() if k in allowed}
    if not updates:
        return
    path = Path(path)
    lines = path.read_text(encoding="utf-8").split("\n")

    sec_idx = None
    for i, line in enumerate(lines):
        if line.rstrip() == "tts:":
            sec_idx = i
            break
    if sec_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("tts:")
        sec_idx = len(lines) - 1

    end = len(lines)
    for i in range(sec_idx + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped and not lines[i][:1].isspace() and not stripped.startswith("#"):
            end = i
            break

    def _fmt(key, value):
        if key == "emotion":
            return _yaml_quote(str(value or ""))
        return str(value)

    replaced = set()
    for i in range(sec_idx + 1, end):
        m = re.match(r"^(\s+)(\w+)\s*:\s*(.*)$", lines[i])
        if not m or m.group(2) not in updates:
            continue
        key = m.group(2)
        comment = _extract_line_comment(m.group(3))
        suffix = f"  {comment}" if comment else ""
        lines[i] = f"{m.group(1)}{key}: {_fmt(key, updates[key])}{suffix}"
        replaced.add(key)
    missing = [f"  {key}: {_fmt(key, updates[key])}"
               for key in ("speed", "vol", "pitch", "emotion")
               if key in updates and key not in replaced]
    lines[sec_idx + 1:sec_idx + 1] = missing

    path.write_text("\n".join(lines), encoding="utf-8")


def cookie_configured(cfg: dict) -> bool:
    """danmaku.cookie 是否已配置（非空）。"""
    return bool(str((cfg.get("danmaku", {}) or {}).get("cookie", "") or "").strip())


# 登录态关键字段（任一命中即视为登录态；命中不了则匿名态，观看限制直播间抓不到事件）
_COOKIE_LOGIN_KEYS = ("sessionid", "sessionid_ss", "sid_tt", "uid_tt", "passport_csrf_token")


def cookie_logged_in(cfg: dict) -> bool:
    """检测 Cookie 是否含登录态关键字段。

    两种形态通用：单行请求头串（k=v; 分隔）与 Netscape cookies.txt（制表符分隔），
    字段名要求词边界，避免 mysessionid 之类误命中。
    """
    raw = str((cfg.get("danmaku", {}) or {}).get("cookie", "") or "")
    if not raw.strip():
        return False
    return any(re.search(rf"(?:^|[\s;]){re.escape(k)}[=\t]", raw)
               for k in _COOKIE_LOGIN_KEYS)


def update_cookie_config(path, cookie: str) -> None:
    """把 danmaku.cookie 写回 config.yaml（行级替换，保留注释与其他配置）。

    cookie 允许两种形态（采集器都能识别）：单行请求头 cookie 值、多行
    Netscape cookies.txt——非空统一落成 | 块标量（每行 4 空格缩进），
    空值落成空串。原值无论行内串还是块标量都整块替换。
    """
    value = str(cookie or "").strip()
    path = Path(path)
    lines = path.read_text(encoding="utf-8").split("\n")
    key_re = re.compile(r"^(\s+)cookie\s*:\s*(.*)$")

    sec_idx = None
    for i, line in enumerate(lines):
        if line.rstrip() == "danmaku:":
            sec_idx = i
            break

    if value:
        new_lines = ["  cookie: |-"] + [f"    {ln}" for ln in value.split("\n")]
    else:
        new_lines = ['  cookie: ""']

    if sec_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("danmaku:")
        lines.extend(new_lines)
    else:
        end = len(lines)
        for i in range(sec_idx + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped and not lines[i][:1].isspace() and not stripped.startswith("#"):
                end = i
                break
        key_idx = None
        for i in range(sec_idx + 1, end):
            m = key_re.match(lines[i])
            if m:
                key_idx = i
                key_indent = len(m.group(1))
                break
        if key_idx is None:
            lines[sec_idx + 1:sec_idx + 1] = new_lines
        else:
            # 旧值跨度：行内值只占本行；块标量吞掉更深缩进的后续行
            span_end = key_idx + 1
            while span_end < end:
                ln = lines[span_end]
                indent = len(ln) - len(ln.lstrip())
                if ln.strip() and indent > key_indent:
                    span_end += 1
                else:
                    break
            lines[key_idx:span_end] = new_lines

    path.write_text("\n".join(lines), encoding="utf-8")


def update_audio_device(path, device: str) -> None:
    """把 audio.device 写回 config.yaml（行级替换，保留注释与格式）。

    控制台切换输出声卡时调用；与 update_tts_voice 同一保注释策略。
    """
    value = str(device or "").strip()
    if not value:
        return
    path = Path(path)
    lines = path.read_text(encoding="utf-8").split("\n")
    key_re = re.compile(r"^(\s+)device\s*:\s*(.*)$")

    sec_idx = None
    for i, line in enumerate(lines):
        if line.rstrip() == "audio:":
            sec_idx = i
            break

    if sec_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("audio:")
        lines.append(f"  device: {_yaml_quote(value)}")
    else:
        end = len(lines)
        for i in range(sec_idx + 1, len(lines)):
            stripped = lines[i].strip()
            if stripped and not lines[i][:1].isspace() and not stripped.startswith("#"):
                end = i
                break
        replaced = False
        for i in range(sec_idx + 1, end):
            m = key_re.match(lines[i])
            if m:
                comment = _extract_line_comment(m.group(2))
                suffix = f"  {comment}" if comment else ""
                lines[i] = f"{m.group(1)}device: {_yaml_quote(value)}{suffix}"
                replaced = True
                break
        if not replaced:
            lines.insert(sec_idx + 1, f"  device: {_yaml_quote(value)}")

    path.write_text("\n".join(lines), encoding="utf-8")
