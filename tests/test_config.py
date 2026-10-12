"""hub.config 校验规则的单元测试（PROMPTS 步骤 0.1）。"""
import json
from pathlib import Path

import pytest
import yaml

from hub import config as cfg_mod


@pytest.fixture
def minimax_secret(tmp_path, monkeypatch):
    """在临时目录伪造 voice_clone_data/config.json 并指向它。"""
    secret_dir = tmp_path / "voice_clone" / "voice_clone_data"
    secret_dir.mkdir(parents=True)
    secret_file = secret_dir / "config.json"
    secret_file.write_text(
        json.dumps({"api_key": "sk-test-minimax-key", "endpoint": "https://api.minimaxi.com"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg_mod, "MINIMAX_SECRET_PATH", secret_file)
    return secret_file


def write_yaml(tmp_path, overrides: dict) -> Path:
    base = {
        "app": {"name": "zaizaishu-live", "log_level": "info", "replay_file": ""},
        "danmaku": {"ws_listen": "127.0.0.1:8765"},
        "llm": {"base_url": "https://api.example.com/v1", "api_key": "sk-llm-key",
                "model": "test-model", "temperature": 0.8, "max_tokens": 120, "timeout_seconds": 8},
        "tts": {"voice_id": "demoVoice001", "model": "speech-2.8-hd",
                "speed": 1.0, "vol": 1.0, "pitch": 0},
        "audio": {"device": "CABLE Input", "sample_rate": 24000, "channels": 1, "gap_ms": 800},
        "persona": {"nickname": "仔仔鼠", "style": "口语化", "max_reply_chars": 40, "nickname_prefix_len": 2},
        "game": {"name": "《示例游戏》", "intro": "一句话", "download_hint": "点点小手柄", "highlights": ["a", "b"]},
        "events": {
            "chat": {"enabled": True, "merge_window_s": 3, "stale_drop_s": 30, "ignore_noise_p": 0.2},
            "gift": {"enabled": True, "combo_timeout_s": 5},
            "follow": {"enabled": True, "cooldown_s": 30, "probability": 0.6},
            "fansclub": {"enabled": True, "cooldown_s": 30, "probability": 0.8},
            "enter": {"enabled": False},
        },
        "schedule": {"enabled": True, "interval_min": 7, "pool": ["话术一", "话术二"]},
        "safety": {"block_words_file": "blockwords.txt", "fallback_reply": "哈哈这个咱们直播间聊哈", "pause_hotkey": "f9"},
        "watchdog": {"fail_threshold": 10},
    }

    def deep_merge(dst, patch):
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                deep_merge(dst[k], v)
            else:
                dst[k] = v

    deep_merge(base, overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(base, allow_unicode=True), encoding="utf-8")
    (tmp_path / "blockwords.txt").write_text("# 词库\n政治\n赌博\n", encoding="utf-8")
    return path


def test_llm_fields_optional_when_empty(minimax_secret, tmp_path):
    """LLM 三项留空不再是启动错误（用户需求：无需配置 LLM 密钥也可打开）。"""
    cfg_path = write_yaml(tmp_path, {"llm": {"api_key": "", "base_url": "", "model": ""}})
    cfg = cfg_mod.load_config(str(cfg_path))   # 不抛 ConfigError
    assert cfg_mod.llm_configured(cfg) is False


def test_llm_placeholder_loads_but_not_configured(minimax_secret, tmp_path):
    """占位值能正常启动，但按未配置处理（弹幕回复停用）。"""
    cfg_path = write_yaml(tmp_path, {"llm": {"api_key": "PLACEHOLDER-待填真实Key",
                                            "model": "PLACEHOLDER-待填模型名"}})
    cfg = cfg_mod.load_config(str(cfg_path))
    assert cfg_mod.llm_configured(cfg) is False


def test_llm_configured_truth_table(minimax_secret, tmp_path):
    """三项全真实才视为已配置；任一缺失/占位即未配置。"""
    cfg = cfg_mod.load_config(str(write_yaml(tmp_path, {})))
    assert cfg_mod.llm_configured(cfg) is True
    for key in ("base_url", "api_key", "model"):
        saved = cfg["llm"][key]
        cfg["llm"][key] = ""
        assert cfg_mod.llm_configured(cfg) is False
        cfg["llm"][key] = "placeholder-x"
        assert cfg_mod.llm_configured(cfg) is False
        cfg["llm"][key] = saved
    assert cfg_mod.llm_configured({}) is False


def test_config_path_recorded(minimax_secret, tmp_path):
    """load_config 记录配置文件绝对路径，供控制台「配置 LLM」写回定位。"""
    cfg_path = write_yaml(tmp_path, {})
    cfg = cfg_mod.load_config(str(cfg_path))
    assert cfg["_config_path"] == str(cfg_path.resolve())


def test_update_llm_config_preserves_comments_and_sections(tmp_path):
    """写回只换 llm 段三行的值：注释、其余段落、段内其他键原样保留。"""
    text = (
        "# 顶部注释\n"
        "app:\n"
        "  name: zaizaishu-live\n"
        "\n"
        "llm:\n"
        "  # 段内注释\n"
        "  base_url: https://old.example.com/v1   # 地址注释\n"
        '  api_key: "PLACEHOLDER-待填真实Key"      # 密钥注释\n'
        '  model: "old-model"                      # 模型注释\n'
        "  temperature: 0.8\n"
        "\n"
        "tts:\n"
        '  voice_id: "demoVoice001"\n'
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    cfg_mod.update_llm_config(p, base_url="https://new.example.com/v1",
                              api_key='sk-new"含引号#特殊', model="new-model")
    out = p.read_text(encoding="utf-8")
    assert "# 顶部注释" in out
    assert "app:\n  name: zaizaishu-live" in out
    assert 'voice_id: "demoVoice001"' in out
    assert "temperature: 0.8" in out
    assert "地址注释" in out and "密钥注释" in out and "模型注释" in out
    data = yaml.safe_load(out)
    assert data["llm"]["base_url"] == "https://new.example.com/v1"
    assert data["llm"]["api_key"] == 'sk-new"含引号#特殊'
    assert data["llm"]["model"] == "new-model"
    assert data["app"]["name"] == "zaizaishu-live"


def test_update_llm_config_old_value_with_hash_no_corruption(tmp_path):
    """旧 base_url 含 # 时二次换值不粘旧值尾巴（双引号标量内的 # 属值本身）。"""
    p = tmp_path / "config.yaml"
    p.write_text('llm:\n  base_url: "https://old.example.com/v1"  # 地址注释\n'
                 '  api_key: "k"\n  model: "m"\n', encoding="utf-8")
    cfg_mod.update_llm_config(p, base_url="https://a.example.com/v1#frag")
    cfg_mod.update_llm_config(p, base_url="https://b.example.com/v1")
    out = p.read_text(encoding="utf-8")
    line = next(l for l in out.split("\n") if "base_url" in l)
    assert "#frag" not in line
    assert "地址注释" in line
    assert yaml.safe_load(out)["llm"]["base_url"] == "https://b.example.com/v1"


def test_update_llm_config_appends_missing_section(tmp_path):
    """文件没有 llm 段时追加到末尾。"""
    p = tmp_path / "config.yaml"
    p.write_text("app:\n  name: x\n", encoding="utf-8")
    cfg_mod.update_llm_config(p, base_url="https://a/v1", api_key="sk-k", model="m1")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["llm"] == {"base_url": "https://a/v1", "api_key": "sk-k", "model": "m1"}


def test_update_llm_config_partial_and_inserts_missing_keys(tmp_path):
    """None 字段不动；段内缺失的键补插（如手写配置漏了 model 行）。"""
    p = tmp_path / "config.yaml"
    p.write_text("llm:\n  api_key: sk-k\n  temperature: 0.8\n", encoding="utf-8")
    cfg_mod.update_llm_config(p, base_url="https://a/v1", model="m1")  # api_key=None
    out = p.read_text(encoding="utf-8")
    assert "api_key: sk-k" in out
    assert "temperature: 0.8" in out
    data = yaml.safe_load(out)
    assert data["llm"] == {"api_key": "sk-k", "temperature": 0.8,
                          "base_url": "https://a/v1", "model": "m1"}


def test_update_llm_config_roundtrip_with_load(minimax_secret, tmp_path):
    """占位配置 → 控制台写回真实值 → load_config 认得且 llm_configured 变 True。"""
    cfg_path = write_yaml(tmp_path, {"llm": {"api_key": "PLACEHOLDER-待填真实Key"}})
    cfg_mod.update_llm_config(cfg_path, base_url="https://api.example.com/v1",
                              api_key="sk-real", model="gpt-4o-mini")
    cfg = cfg_mod.load_config(str(cfg_path))
    assert cfg_mod.llm_configured(cfg) is True
    assert cfg["llm"]["api_key"] == "sk-real"


def test_full_config_passes(minimax_secret, tmp_path):
    cfg_path = write_yaml(tmp_path, {})
    cfg = cfg_mod.load_config(str(cfg_path))
    assert cfg["tts"]["api_key"] == "sk-test-minimax-key"
    assert cfg["tts"]["endpoint"] == "https://api.minimaxi.com"


def test_minimax_secret_missing_still_boots(tmp_path, monkeypatch):
    """MiniMax 密钥文件缺失不再是启动错误（用户需求：无需 Key 也可打开）。

    未配置 → minimax_configured False（播放侧退打印模式），控制台「音色工坊」可配置。
    """
    monkeypatch.setattr(cfg_mod, "MINIMAX_SECRET_PATH", tmp_path / "nope" / "config.json")
    cfg_path = write_yaml(tmp_path, {})
    cfg = cfg_mod.load_config(str(cfg_path))
    assert cfg_mod.minimax_configured(cfg) is False
    assert cfg["tts"]["api_key"] == ""


def test_minimax_configured_truth_table(minimax_secret, tmp_path):
    """minimax_configured：api_key 非空即已配置（load_config 已合并密钥文件）。"""
    cfg = cfg_mod.load_config(str(write_yaml(tmp_path, {})))
    assert cfg_mod.minimax_configured(cfg) is True
    cfg["tts"]["api_key"] = "  "
    assert cfg_mod.minimax_configured(cfg) is False
    assert cfg_mod.minimax_configured({}) is False


def test_numeric_range_error_is_specific(minimax_secret, tmp_path):
    cfg_path = write_yaml(tmp_path, {"events": {"chat": {"merge_window_s": 99}}})
    with pytest.raises(cfg_mod.ConfigError) as exc:
        cfg_mod.load_config(str(cfg_path))
    assert "merge_window_s" in str(exc.value)
    assert "1~10" in str(exc.value)


def test_numeric_type_error(minimax_secret, tmp_path):
    cfg_path = write_yaml(tmp_path, {"events": {"chat": {"stale_drop_s": "abc"}}})
    with pytest.raises(cfg_mod.ConfigError) as exc:
        cfg_mod.load_config(str(cfg_path))
    assert "stale_drop_s" in str(exc.value)
    assert "类型" in str(exc.value)


def test_stale_drop_p1_s_validated(minimax_secret, tmp_path):
    """P1 回复专用过期阈值：类型与范围（10~300）都要校验。"""
    cfg_path = write_yaml(tmp_path, {"events": {"chat": {"stale_drop_p1_s": "abc"}}})
    with pytest.raises(cfg_mod.ConfigError) as exc:
        cfg_mod.load_config(str(cfg_path))
    assert "stale_drop_p1_s" in str(exc.value)

    cfg_path = write_yaml(tmp_path, {"events": {"chat": {"stale_drop_p1_s": 999}}})
    with pytest.raises(cfg_mod.ConfigError) as exc:
        cfg_mod.load_config(str(cfg_path))
    assert "stale_drop_p1_s" in str(exc.value)

    # 90 是合法值（本次放宽的目标值）
    cfg_path = write_yaml(tmp_path, {"events": {"chat": {"stale_drop_p1_s": 90}}})
    cfg = cfg_mod.load_config(str(cfg_path))
    assert cfg["events"]["chat"]["stale_drop_p1_s"] == 90


def test_stale_drop_p2_s_validated(minimax_secret, tmp_path):
    """P2 口播专用过期阈值：类型与范围（10~600）都要校验。"""
    cfg_path = write_yaml(tmp_path, {"events": {"chat": {"stale_drop_p2_s": "abc"}}})
    with pytest.raises(cfg_mod.ConfigError) as exc:
        cfg_mod.load_config(str(cfg_path))
    assert "stale_drop_p2_s" in str(exc.value)

    cfg_path = write_yaml(tmp_path, {"events": {"chat": {"stale_drop_p2_s": 9999}}})
    with pytest.raises(cfg_mod.ConfigError) as exc:
        cfg_mod.load_config(str(cfg_path))
    assert "stale_drop_p2_s" in str(exc.value)

    # 180 是合法值（P2 让位积压需挨过当前在播的最长话题）
    cfg_path = write_yaml(tmp_path, {"events": {"chat": {"stale_drop_p2_s": 180}}})
    cfg = cfg_mod.load_config(str(cfg_path))
    assert cfg["events"]["chat"]["stale_drop_p2_s"] == 180


def test_replay_file_missing(minimax_secret, tmp_path):
    cfg_path = write_yaml(tmp_path, {"app": {"replay_file": "replay/no-such-file.jsonl"}})
    with pytest.raises(cfg_mod.ConfigError) as exc:
        cfg_mod.load_config(str(cfg_path))
    assert "replay_file" in str(exc.value)


def test_voice_id_missing_still_boots(minimax_secret, tmp_path):
    """tts.voice_id 留空不再是启动错误（模板默认值兜底，控制台音色工坊可设置）。"""
    cfg_path = write_yaml(tmp_path, {"tts": {"voice_id": ""}})
    cfg = cfg_mod.load_config(str(cfg_path))
    assert cfg["tts"]["voice_id"] == ""
    assert cfg_mod.minimax_configured(cfg) is True


def test_update_tts_voice_preserves_comments_and_sections(tmp_path):
    """音色工坊「设为直播音色」写回：只换 tts.voice_id 行，注释与其余配置原样保留。"""
    text = (
        "app:\n"
        "  name: zaizaishu-live\n"
        "\n"
        "tts:\n"
        "  # 音色 ID 由控制台音色工坊维护\n"
        '  voice_id: "demoVoice001"        # 旧音色\n'
        "  model: speech-2.8-hd\n"
        "  speed: 1.0\n"
        "\n"
        "audio:\n"
        "  sample_rate: 24000\n"
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    cfg_mod.update_tts_voice(p, "zsNewVoice99")
    out = p.read_text(encoding="utf-8")
    assert "音色 ID 由控制台音色工坊维护" in out
    assert "旧音色" in out                      # 行尾注释保留
    assert "model: speech-2.8-hd" in out
    assert "sample_rate: 24000" in out
    data = yaml.safe_load(out)
    assert data["tts"]["voice_id"] == "zsNewVoice99"
    assert data["tts"]["model"] == "speech-2.8-hd"
    assert data["app"]["name"] == "zaizaishu-live"


def test_update_tts_voice_inserts_and_appends_section(tmp_path):
    """tts 段缺 voice_id 行时补插；整段缺失时追加。空值直接忽略（防误清）。"""
    p = tmp_path / "config.yaml"
    p.write_text("tts:\n  model: m1\n", encoding="utf-8")
    cfg_mod.update_tts_voice(p, "voiceA111")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["tts"] == {"model": "m1", "voice_id": "voiceA111"}

    p2 = tmp_path / "b.yaml"
    p2.write_text("app:\n  name: x\n", encoding="utf-8")
    cfg_mod.update_tts_voice(p2, "voiceB222")
    data2 = yaml.safe_load(p2.read_text(encoding="utf-8"))
    assert data2["tts"] == {"voice_id": "voiceB222"}
    assert data2["app"]["name"] == "x"

    before = p2.read_text(encoding="utf-8")
    cfg_mod.update_tts_voice(p2, "")
    assert p2.read_text(encoding="utf-8") == before


def test_blockwords_file_missing(minimax_secret, tmp_path):
    cfg_path = write_yaml(tmp_path, {"safety": {"block_words_file": "no-such-words.txt"}})
    with pytest.raises(cfg_mod.ConfigError) as exc:
        cfg_mod.load_config(str(cfg_path))
    assert "block_words_file" in str(exc.value)


def test_blockwords_no_valid_words(minimax_secret, tmp_path):
    cfg_path = write_yaml(tmp_path, {})
    (tmp_path / "blockwords.txt").write_text("# 只有注释\n# 没有词条\n", encoding="utf-8")
    with pytest.raises(cfg_mod.ConfigError) as exc:
        cfg_mod.load_config(str(cfg_path))
    assert "无有效词条" in str(exc.value)


def test_blockwords_path_resolved_absolute(minimax_secret, tmp_path):
    cfg_path = write_yaml(tmp_path, {})
    cfg = cfg_mod.load_config(str(cfg_path))
    resolved = cfg["safety"]["block_words_file"]
    assert Path(resolved).is_absolute()
    assert Path(resolved).exists()


def test_update_safety_config_preserves_comments_and_sections(tmp_path):
    """兜底口播写回：只换 safety.fallback_reply / fallback_enabled，注释与其余配置保留。"""
    text = (
        "safety:\n"
        "  block_words_file: blockwords.txt\n"
        '  fallback_reply: "哈哈这个咱们直播间聊哈"   # 旧文案\n'
        "  pause_hotkey: f9\n"
        "\n"
        "watchdog:\n"
        "  silent_minutes: 10\n"
    )
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    cfg_mod.update_safety_config(p, fallback_reply="新兜底", fallback_enabled=False)
    out = p.read_text(encoding="utf-8")
    assert "block_words_file: blockwords.txt" in out
    assert "旧文案" in out                      # 行尾注释保留
    assert "pause_hotkey: f9" in out
    assert "silent_minutes: 10" in out
    data = yaml.safe_load(out)
    assert data["safety"]["fallback_reply"] == "新兜底"
    assert data["safety"]["fallback_enabled"] is False


def test_mask_secret():
    assert cfg_mod.mask_secret("sk-abcd1234") == "sk-a****"
    assert cfg_mod.mask_secret("") == "<空>"
    assert cfg_mod.mask_secret(None) == "<空>"


# ---------- script 指针（话术管理 v3.1） ----------

def test_script_settings_defaults_and_invalid_scheme():
    assert cfg_mod.script_settings({}) == {"scheme": "ai", "active_profile": ""}
    assert cfg_mod.script_settings({"script": {"scheme": "weird", "active_profile": " p_1 "}}) \
        == {"scheme": "ai", "active_profile": "p_1"}
    assert cfg_mod.script_settings({"script": {"scheme": "template",
                                               "active_profile": "p_abc"}}) \
        == {"scheme": "template", "active_profile": "p_abc"}


def test_update_script_config_appends_section_when_missing(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("app:\n  name: x  # 应用名\n", encoding="utf-8")
    cfg_mod.update_script_config(p, scheme="template", active_profile="p_ab12")
    text = p.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert data["script"] == {"scheme": "template", "active_profile": "p_ab12"}
    assert data["app"]["name"] == "x"
    assert "# 应用名" in text  # 注释保留


def test_update_script_config_replaces_and_keeps_others(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "script:\n"
        "  scheme: ai          # 当前方案\n"
        "  active_profile: p_old\n"
        "audio:\n  device: \"X\"  # 声卡\n", encoding="utf-8")
    cfg_mod.update_script_config(p, active_profile="p_new")
    text = p.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert data["script"] == {"scheme": "ai", "active_profile": "p_new"}
    assert "# 当前方案" in text
    assert data["audio"]["device"] == "X"
    # 再改 scheme，active_profile 保持
    cfg_mod.update_script_config(p, scheme="template")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["script"] == {"scheme": "template", "active_profile": "p_new"}


def test_update_script_config_noop_when_all_none(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("app:\n  name: x\n", encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    cfg_mod.update_script_config(p)
    assert p.read_text(encoding="utf-8") == before

# ---------- v3.1：Cookie 卡写回（danmaku.cookie，块标量保注释替换） ----------

def test_cookie_configured(tmp_path):
    assert cfg_mod.cookie_configured({}) is False
    assert cfg_mod.cookie_configured({"danmaku": {"cookie": "  "}}) is False
    assert cfg_mod.cookie_configured({"danmaku": {"cookie": "a=1"}}) is True


def test_update_cookie_config_replaces_block_scalar(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "danmaku:\n"
        "  room_num: \"123\"\n"
        "  # cookie 注释行\n"
        "  cookie: |\n"
        "    .douyin.com\tFALSE\t/\tTRUE\t0\told_key\told_val\n"
        "    .douyin.com\tFALSE\t/\tFALSE\t0\told2\tv2\n"
        "  ws_listen: 127.0.0.1:8765\n"
        "app:\n  name: x\n", encoding="utf-8")
    cfg_mod.update_cookie_config(p, "new_key=new_val; other=1")
    text = p.read_text(encoding="utf-8")
    assert "  cookie: |-\n    new_key=new_val; other=1\n" in text
    assert "old_key" not in text                    # 旧块标量被整块替换
    assert "  ws_listen: 127.0.0.1:8765\n" in text   # 后续键不受影响
    assert "# cookie 注释行" in text                 # 注释保留
    data = yaml.safe_load(text)
    assert data["danmaku"]["cookie"] == "new_key=new_val; other=1"
    assert data["danmaku"]["room_num"] == "123"


def test_update_cookie_config_replaces_inline_and_multiline(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text('danmaku:\n  cookie: "a=1"\n  room_num: ""\n', encoding="utf-8")
    cfg_mod.update_cookie_config(p, ".douyin.com\tFALSE\t/\tTRUE\t0\tttwid\tx\n"
                                    ".douyin.com\tFALSE\t/\tFALSE\t0\tfpk2\ty")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    lines = data["danmaku"]["cookie"].split("\n")
    assert len(lines) == 2 and lines[0].endswith("ttwid\tx")


def test_update_cookie_config_clear_and_missing_section(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("danmaku:\n  cookie: |\n    abc\n", encoding="utf-8")
    cfg_mod.update_cookie_config(p, "")
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["danmaku"]["cookie"] == ""
    # 缺 danmaku 段：追加到文件末尾
    p2 = tmp_path / "c2.yaml"
    p2.write_text("app:\n  name: x\n", encoding="utf-8")
    cfg_mod.update_cookie_config(p2, "k=v")
    assert yaml.safe_load(p2.read_text(encoding="utf-8"))["danmaku"]["cookie"] == "k=v"

# ---------- v4 / M3：智能体段（DESIGN §12.1，总开关与模式切换） ----------

AGENT_YAML = """\
# 顶注
server:
  host: 0.0.0.0
  port: 8766

agent:
  enabled: false        # 总开关
  retrieve:
    layer: l2           # 检索层
    top_k: 5
  emotion:
    enabled: true
  decision_log: true

script:
  scheme: ai
"""


def test_agent_settings_defaults():
    """空配置取出厂默认：关、l2、top_k 5、情绪与反馈默认开。"""
    st = cfg_mod.agent_settings({})
    assert st["enabled"] is False
    assert st["retrieve"] == {"layer": "l2", "top_k": 5}
    assert st["memory"] == {"max_events": 50, "max_spoken": 20, "max_notes": 10}
    assert st["feedback"] == {"enabled": True}
    assert st["emotion"] == {"enabled": True, "onsite_fallback": True}
    assert st["decision_log"] is True


def test_agent_settings_clamps_and_falls_back():
    """越界夹取、枚举非法回落、字符串真假按宽松词表（off/no/0 都算 False）。"""
    st = cfg_mod.agent_settings({"agent": {
        "enabled": "yes",
        "retrieve": {"layer": "L9", "top_k": 999},
        "memory": {"max_events": -3, "max_notes": 0},
        "emotion": {"enabled": "off", "onsite_fallback": "no"},
        "feedback": {"enabled": "FALSE"},
        "decision_log": "0",
    }})
    assert st["enabled"] is True
    assert st["retrieve"] == {"layer": "l2", "top_k": 20}   # l9 非法回落，999 夹到上界
    assert st["memory"]["max_events"] == 1                  # 夹到下界
    assert st["memory"]["max_notes"] == 0                   # 0 是合法下界，不夹到 1
    assert st["emotion"] == {"enabled": False, "onsite_fallback": False}
    assert st["feedback"]["enabled"] is False
    assert st["decision_log"] is False
    # 合法枚举保留、上界不越
    st2 = cfg_mod.agent_settings({"agent": {"retrieve": {"layer": "l1"},
                                            "memory": {"max_spoken": 200}}})
    assert st2["retrieve"]["layer"] == "l1" and st2["memory"]["max_spoken"] == 200


def test_agent_status_requires_llm():
    """V3.3：未配 LLM 时开关可读但 available=False（UI 据此置灰）。"""
    st = cfg_mod.agent_status({})
    assert set(st) == {"enabled", "available", "llm_configured", "settings"}
    assert st["available"] is False and st["llm_configured"] is False
    assert st["enabled"] is False
    assert cfg_mod.agent_available({}) is cfg_mod.llm_configured({})


def test_agent_config_validated(minimax_secret, tmp_path):
    """agent 段校验：数值越界、枚举非法、布尔键类型错都要报出来。"""
    for patch, needle in (
        ({"agent": {"retrieve": {"top_k": 99}}}, "agent.retrieve.top_k"),
        ({"agent": {"retrieve": {"layer": "l9"}}}, "agent.retrieve.layer"),
        ({"agent": {"enabled": "maybe"}}, "agent.enabled"),
        ({"agent": {"memory": {"max_notes": -1}}}, "agent.memory.max_notes"),
    ):
        cfg_path = write_yaml(tmp_path, patch)
        with pytest.raises(cfg_mod.ConfigError) as exc:
            cfg_mod.load_config(str(cfg_path))
        assert needle in str(exc.value), (patch, str(exc.value))

    # 合法值不产生 agent 相关错误
    cfg_path = write_yaml(tmp_path, {"agent": {
        "enabled": True, "retrieve": {"layer": "l1", "top_k": 5},
        "memory": {"max_events": 50, "max_spoken": 20, "max_notes": 10},
        "emotion": {"enabled": True, "onsite_fallback": True},
        "feedback": {"enabled": True}, "decision_log": True}})
    cfg = cfg_mod.load_config(str(cfg_path))
    assert not [e for e in cfg_mod.validate_config(cfg, tmp_path) if "agent" in e]


def test_update_agent_config_preserves_comments_and_siblings(tmp_path):
    """行级写回：注释保留、同级同名的 agent.emotion.enabled 不被误改。"""
    p = tmp_path / "c.yaml"
    p.write_text(AGENT_YAML, encoding="utf-8")
    cfg_mod.update_agent_config(p, {"enabled": True, "retrieve.layer": "l3"})
    text = p.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert data["agent"]["enabled"] is True
    assert data["agent"]["retrieve"] == {"layer": "l3", "top_k": 5}
    assert data["agent"]["emotion"]["enabled"] is True       # 同名不同层，未被改
    assert data["script"] == {"scheme": "ai"} and data["server"]["port"] == 8766
    assert "# 总开关" in text and "# 检索层" in text and "# 顶注" in text
    assert '"l3"' not in text                                 # 枚举裸写，不加引号
    # 幂等
    before = text
    cfg_mod.update_agent_config(p, {"enabled": True, "retrieve.layer": "l3"})
    assert p.read_text(encoding="utf-8") == before


def test_update_agent_config_appends_section_and_parent_chain(tmp_path):
    """无 agent 段时追加最小可用段；缺父链（memory）时补齐且不重复键。"""
    p = tmp_path / "c.yaml"
    p.write_text("""app:
  name: x  # 应用名
""", encoding="utf-8")
    cfg_mod.update_agent_config(p, {"enabled": True})
    text = p.read_text(encoding="utf-8")
    assert yaml.safe_load(text)["agent"] == {"enabled": True}
    assert text.count("enabled:") == 1
    assert "# 应用名" in text

    p2 = tmp_path / "c2.yaml"
    p2.write_text(AGENT_YAML, encoding="utf-8")
    cfg_mod.update_agent_config(p2, {"memory.max_notes": 7})
    data = yaml.safe_load(p2.read_text(encoding="utf-8"))
    assert data["agent"]["memory"] == {"max_notes": 7}
    assert data["agent"]["enabled"] is False                  # 其余键不动
    assert data["script"] == {"scheme": "ai"}
    # 新键插在段内，不与下一段之间多出空行
    assert """  memory:
    max_notes: 7

script:""" in p2.read_text(encoding="utf-8")


def test_update_agent_config_whitelist_and_noop(tmp_path):
    """非白名单键丢弃（防止把任意键塞进用户配置）；全 None 时不动文件。"""
    p = tmp_path / "c.yaml"
    p.write_text(AGENT_YAML, encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    cfg_mod.update_agent_config(p, {"enabled": None})
    assert p.read_text(encoding="utf-8") == before
    cfg_mod.update_agent_config(p, {"enabled": True, "evil.key": 1, "llm.api_key": "sk-x"})
    text = p.read_text(encoding="utf-8")
    assert "evil" not in text and "api_key" not in text
    assert yaml.safe_load(text)["agent"]["enabled"] is True


# ---------- M6：对话调参用的 schedule 写回（行级替换保注释 + 越界当场拒） ----------

SCHEDULE_YAML = """\
schedule:
  enabled: true
  interval_min: 7          # 实际取 7±1 分钟随机
  pool:
    - "兄弟们点点关注不迷路"
    - "想玩同款的点点小手柄"

audio:
  device: "X"   # 声卡
"""


def test_update_schedule_config_replaces_minutes_and_keeps_comment(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(SCHEDULE_YAML, encoding="utf-8")
    cfg_mod.update_schedule_config(p, interval_min=15)
    text = p.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    assert data["schedule"]["interval_min"] == 15
    assert data["schedule"]["pool"] == ["兄弟们点点关注不迷路", "想玩同款的点点小手柄"]
    assert data["schedule"]["enabled"] is True
    assert "# 实际取 7±1 分钟随机" in text       # 行尾注释保留
    assert "# 声卡" in text                     # 别段的注释也没被动


def test_update_schedule_config_does_not_touch_pool_lines(tmp_path):
    """pool 是列表，若按前缀匹配很容易误伤 —— 只认缩进 2 空格的那一层键。"""
    p = tmp_path / "c.yaml"
    p.write_text(SCHEDULE_YAML, encoding="utf-8")
    cfg_mod.update_schedule_config(p, interval_min=3, enabled=False)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert data["schedule"] == {"enabled": False, "interval_min": 3,
                                "pool": ["兄弟们点点关注不迷路", "想玩同款的点点小手柄"]}


def test_update_schedule_config_appends_section_when_missing(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("app:\n  name: x  # 应用名\n", encoding="utf-8")
    cfg_mod.update_schedule_config(p, interval_min=5)
    text = p.read_text(encoding="utf-8")
    assert yaml.safe_load(text)["schedule"]["interval_min"] == 5
    assert yaml.safe_load(text)["app"]["name"] == "x" and "# 应用名" in text


@pytest.mark.parametrize("bad", [0, -1, 121, 999])
def test_update_schedule_config_rejects_out_of_range(tmp_path, bad):
    """越界写下去会让应用下次启动 validate_config 失败 —— 宁可当场报错。"""
    p = tmp_path / "c.yaml"
    p.write_text(SCHEDULE_YAML, encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        cfg_mod.update_schedule_config(p, interval_min=bad)
    assert p.read_text(encoding="utf-8") == before


def test_update_schedule_config_rejects_non_number(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(SCHEDULE_YAML, encoding="utf-8")
    with pytest.raises(ValueError):
        cfg_mod.update_schedule_config(p, interval_min="十分钟")


def test_update_schedule_config_noop_when_all_none(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(SCHEDULE_YAML, encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    cfg_mod.update_schedule_config(p)
    assert p.read_text(encoding="utf-8") == before


def test_update_schedule_config_result_passes_validation(tmp_path):
    """写回后的文件要能过自家校验（不写"下次启动就炸"的配置）。"""
    p = tmp_path / "c.yaml"
    p.write_text(SCHEDULE_YAML, encoding="utf-8")
    cfg_mod.update_schedule_config(p, interval_min=60, enabled=True)
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    errors = cfg_mod.validate_config(data, tmp_path)
    # 只看 schedule.* 的报错：临时目录名里也带 "schedule_config"，
    # 按普通子串过滤会把「词库缺失」这类无关项一起捞进来
    assert not [e for e in errors if "schedule." in str(e)], errors
