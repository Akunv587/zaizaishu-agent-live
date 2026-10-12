# -*- mode: python ; coding: utf-8 -*-
"""仔仔鼠AI智能体直播助手 · 单文件 exe 打包配置。

构建：.venv\\Scripts\\python.exe -m PyInstaller zaizaishu.spec --noconfirm
产物：dist/zaizaishu.exe（Win11 自带 WebView2，无需额外安装）

资源布局（hub/paths.py 双锚点）：
- 随包内置（_MEIPASS）：webui/index.html、voice_clone/minimax_client.py、
  config.example.yaml、blockwords.example.txt
- exe 旁读写（用户数据）：config.yaml、logs/、blockwords.txt、
  voice_clone/voice_clone_data/、desktop_state.json
（首跑由 desktop.py 自动把两个模板落地到 exe 旁）
"""
from pathlib import Path

ROOT = Path(SPECPATH).resolve()

a = Analysis(
    ['desktop.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        ('webui/index.html', 'webui'),
        ('voice_clone/minimax_client.py', 'voice_clone'),
        ('config.example.yaml', '.'),
        ('blockwords.example.txt', '.'),
        # 注意：**种子存档不进 exe**。v4 种子（3 情绪档）已到 ~69 MB，内联会让 exe
        # 从 ~23 MB 膨胀到 ~92 MB（且每次启动都要把它解包到临时目录）。
        # 发布包把它放在 exe 旁的 assets/seed_profile.zip，首跑由
        # hub.scriptstore.find_seed_zip() 找到并自动落地（见 docs/IMPLEMENTATION.md §四）。
    ],
    hiddenimports=[
        # pywebview 在 guilib 里按平台动态挑后端，静态分析兜不住
        'webview.platforms.winforms',
        'webview.platforms.edgechromium',
        # plyer 桌面通知的 Windows 后端（无官方 hook，缺了只是通知静默）
        'plyer.platforms.win.notification',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'pytest', 'PIL',           # 构建/测试期依赖，运行时不用
        'webview.platforms.gtk', 'webview.platforms.qt',
        'webview.platforms.cocoa', 'webview.platforms.android',
        'webview.platforms.mshtml',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='zaizaishu-agent',
    icon='assets/icon.ico',
    console=False,
    uac_admin=False,
    onefile=True,
)
