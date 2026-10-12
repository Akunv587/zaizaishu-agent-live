"""输出设备名解析与列表（hub.webui.resolve_output_device / _list_output_devices）。

背景：同一物理设备在 MME（31 字符截断名）/DirectSound/WASAPI 下同名或多条目，
按名打开流会报 "Multiple output devices found"；且实测仅 MME 组合 24000Hz 可用
（DirectSound 静音、WASAPI 报 Invalid sample rate）。这组测试锁定：
- 带 hostapi 后缀的新格式精确解析；
- 旧 config 裸名（完整名/截断名）解析时 MME 全局优先；
- 设备下拉只列 MME 条目；
- update_audio_device 写回：注释保留 + 含 #/CRLF 设备名不腐蚀配置行。
"""
import sys
import types

import pytest
import yaml

from hub.config import update_audio_device
from hub.webui import _list_output_devices, resolve_output_device


def _install_fake_sd(monkeypatch, devices, hostapis):
    fake = types.ModuleType("sounddevice")
    fake.query_devices = lambda: devices
    fake.query_hostapis = lambda idx: {"name": hostapis[idx]}
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    return fake


# 模拟真实机器的 CABLE 三 API 布局：MME 名称被截断到 31 字符
_DEVICES = [
    {"name": "扬声器 (Realtek High Definiti", "max_output_channels": 2, "hostapi": 0},
    {"name": "扬声器 (Realtek High Definition Audio)", "max_output_channels": 2, "hostapi": 1},
    {"name": "CABLE Input (VB-Audio Virtual C", "max_output_channels": 2, "hostapi": 0},
    {"name": "CABLE Input (VB-Audio Virtual Cable)", "max_output_channels": 2, "hostapi": 1},
    {"name": "CABLE Input (VB-Audio Virtual Cable)", "max_output_channels": 2, "hostapi": 2},
    {"name": "麦克风 (仅输入不可选)", "max_output_channels": 0, "hostapi": 0},
]
_HOSTAPIS = ["MME", "Windows DirectSound", "Windows WASAPI"]
_CABLE_MME_IDX = 2


@pytest.fixture
def fake_sd(monkeypatch):
    return _install_fake_sd(monkeypatch, _DEVICES, _HOSTAPIS)


def test_suffixed_name_exact_match(fake_sd):
    assert resolve_output_device("CABLE Input (VB-Audio Virtual Cable), Windows DirectSound") == 3


def test_bare_full_name_prefers_mme(fake_sd):
    """旧 exe 下拉写入的完整裸名：DirectSound/WASAPI 精确命中，但必须选 MME 截断名条目。"""
    assert resolve_output_device("CABLE Input (VB-Audio Virtual Cable)") == _CABLE_MME_IDX


def test_truncated_mme_name_exact(fake_sd):
    """历史 config 的 MME 截断名（勿补全括号）：精确命中 MME。"""
    assert resolve_output_device("CABLE Input (VB-Audio Virtual C") == _CABLE_MME_IDX


def test_speaker_bare_name_prefers_mme(fake_sd):
    assert resolve_output_device("扬声器 (Realtek High Definition Audio)") == 0


def test_unknown_name_raises(fake_sd):
    with pytest.raises(ValueError):
        resolve_output_device("不存在的设备")


def test_device_list_mme_only(fake_sd, monkeypatch):
    """下拉只列 MME 条目（能用组合），纯输入设备排除。"""
    import hub.webui as w
    monkeypatch.setattr(w, "_output_devices_cache", (0.0, []))
    got = _list_output_devices(refresh=True)
    assert got == ["扬声器 (Realtek High Definiti, MME", "CABLE Input (VB-Audio Virtual C, MME"]


# ── update_audio_device 写回（行级替换保注释）：防「值含 #」腐蚀 ──

def test_update_audio_device_roundtrip_keeps_comment(tmp_path):
    """换值后行尾注释保留，YAML 解析值即新设备。"""
    p = tmp_path / "config.yaml"
    p.write_text('audio:\n  device: "CABLE Input (VB-Audio Virtual C, MME"  # 声卡注释\n'
                 "  sample_rate: 24000\n", encoding="utf-8")
    update_audio_device(p, "扬声器 (Realtek High Definiti, MME")
    out = p.read_text(encoding="utf-8")
    line = next(l for l in out.split("\n") if l.strip().startswith("device:"))
    assert "声卡注释" in line
    assert yaml.safe_load(out)["audio"]["device"] == "扬声器 (Realtek High Definiti, MME"


def test_update_audio_device_hash_crlf_name_no_corruption(tmp_path):
    """含 # 与真实 CRLF 的设备名（蓝牙 Hands-Free mangled 名）写入再换值不粘旧值尾巴。

    旧实现 partition("#") 把双引号值内的 # 当注释起点，旧值尾巴被当作「注释」
    粘进新行且逐次累积（本机 config.yaml device 行真实事故）。
    """
    poison = ("耳机 (@System32\\drivers\\bthhfenum.sys,#2;%1 Hands-Free%0\r\n;"
              "(StarRing Ultra)), Windows WDM-KS")
    p = tmp_path / "config.yaml"
    p.write_text('audio:\n  device: "CABLE Input (VB-Audio Virtual C, MME"  # 声卡注释\n'
                 "  sample_rate: 24000\n", encoding="utf-8")
    update_audio_device(p, poison)
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["audio"]["device"] == poison
    update_audio_device(p, "扬声器 (Realtek High Definiti, MME")
    update_audio_device(p, "CABLE Input (VB-Audio Virtual C, MME")
    out = p.read_text(encoding="utf-8")
    line = next(l for l in out.split("\n") if l.strip().startswith("device:"))
    assert "Hands-Free" not in line  # 旧值尾巴没被当作注释粘进来
    assert "声卡注释" in line
    assert yaml.safe_load(out)["audio"]["device"] == "CABLE Input (VB-Audio Virtual C, MME"


def test_update_audio_device_bare_value_comment(tmp_path):
    """手填裸值（未加引号）的行尾注释同样保留：YAML 规定注释 # 前必有空白，按 ' #' 切。"""
    p = tmp_path / "config.yaml"
    p.write_text("audio:\n  device: CABLE Input  # 裸值注释\n", encoding="utf-8")
    update_audio_device(p, "扬声器 (Realtek High Definiti, MME")
    out = p.read_text(encoding="utf-8")
    assert "裸值注释" in out
    assert yaml.safe_load(out)["audio"]["device"] == "扬声器 (Realtek High Definiti, MME"
