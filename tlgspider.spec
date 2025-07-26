# -*- mode: python ; coding: utf-8 -*-
import sys
import platform

# 系统识别
IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform.startswith("darwin")
ARCH = platform.machine().lower()

# 平台相关参数
STRIP = not IS_WINDOWS
ICON = 'icon.ico' if IS_WINDOWS else ('icon.icns' if IS_MAC else None)
CONSOLE = True
TARGET_ARCH = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='tlgspider',
    debug=False,
    bootloader_ignore_signals=False,
    strip=STRIP,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=TARGET_ARCH,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON
)
