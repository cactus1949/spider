# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for recon tool
# 打包命令（在仓库根目录或 tools/ 目录执行均可）：
#     pyinstaller tools/recon.spec --noconfirm
#
# 设计要点：
# - --onedir 模式（spec 中 EXE+COLLECT 即等价）。Playwright chromium 体积约 200MB，
#   不适合 onefile 自解压。
# - pathex 包含 ``tools`` 与 ``src``，因为 recon_tool 会 import spider_gd。
# - 打包后的 exe **不内置 Chromium**。现场需要：
#   1) 在 dist/recon/ 同级创建 ``playwright-browsers/`` 并放置 chromium，
#      或者由 ``run_recon.bat`` 设置 ``PLAYWRIGHT_BROWSERS_PATH`` 指向该目录；
#   2) 或在打包机器上执行 ``playwright install chromium``，然后将
#      ``%LOCALAPPDATA%\ms-playwright\chromium-XXXX`` 拷贝到现场机器对应位置。

import os
import sys

block_cipher = None

# 计算路径：spec 文件位于 tools/，仓库根在其上一级。
HERE = os.path.dirname(os.path.abspath(SPEC))  # type: ignore[name-defined]
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src")
TOOLS = HERE

a = Analysis(
    [os.path.join(TOOLS, "recon_tool", "cli.py")],
    pathex=[TOOLS, SRC],
    binaries=[],
    datas=[],
    hiddenimports=[
        "recon_tool",
        "recon_tool.browser_recon",
        "recon_tool.network_recon",
        "recon_tool.har_analyzer",
        "recon_tool.report",
        "recon_tool.packer",
        "recon_tool.doctor",
        "spider_gd",
        "spider_gd.infrastructure.network.diagnoser",
        "spider_gd.infrastructure.network._powershell",
        "spider_gd.interfaces.network",
        "loguru",
        "pydantic",
        "playwright",
        "playwright.async_api",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="recon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="recon",
)
