"""源码运行与 PyInstaller 冻结运行的双模式路径锚点。

- EXE_DIR：用户数据锚点——config.yaml、logs/、blockwords.txt、
  voice_clone/voice_clone_data/、desktop_state.json 都在这里读写。
  源码运行＝项目根目录；打包后＝exe 所在目录（双击运行时即"干净目录"）。
- BUNDLE_DIR：随包内置的只读资源（webui/index.html、voice_clone/
  minimax_client.py、config.example.yaml）。源码运行＝项目根目录；
  打包后＝PyInstaller 单文件解包目录 _MEIPASS。
"""
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    EXE_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", sys.executable)).resolve()
else:
    EXE_DIR = Path(__file__).resolve().parent.parent
    BUNDLE_DIR = EXE_DIR

# 日志目录：冻结时锚定 exe 旁（快捷方式的"起始位置"不可靠）；源码运行保持
# CWD 相对路径（与既有行为/测试一致）。
LOGS_DIR = EXE_DIR / "logs" if FROZEN else Path("logs")

# 冻结后默认配置文件：exe 同目录，而非 CWD。
DEFAULT_CONFIG = str(EXE_DIR / "config.yaml") if FROZEN else "config.yaml"
