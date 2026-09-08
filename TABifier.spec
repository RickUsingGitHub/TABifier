# -*- mode: python ; coding: utf-8 -*-
"""Freeze TABifier.

Windows: one-file TABifier.exe
  python -m PyInstaller --noconfirm --clean TABifier.spec

Mac: TABifier.app (onedir bundle). Built on GitHub Actions macos-latest.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

settings_file = (
    "settings.json" if Path("settings.json").is_file()
    else "settings.example.json"
)
datas = [
    ("kofi_qr.png", "."),
    ("kofi_button.png", "."),
    (settings_file, "."),
]
binaries = []
hiddenimports = [
    "tkinterdnd2",
    "numpy",
    "PIL",
    "PIL.Image",
    "PIL.ImageOps",
    "pypdf",
    "pdfplumber",
    "pdfminer",
    "pypdfium2",
    "pypdfium2_raw",
    "sheet2tab",
    "tab_model",
    "tab_read",
    "tab_write",
    "svg_page",
]
if sys.platform == "win32":
    hiddenimports.insert(0, "windnd")

for pkg in ("tkinterdnd2", "pdfplumber", "pypdfium2", "pypdfium2_raw"):
    try:
        d, b, h = collect_all(pkg)
    except Exception:
        continue
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["tabmaker_gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    icns = "tabifier.icns" if Path("tabifier.icns").is_file() else None
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="TABifier",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=True,
        target_arch=None,
        codesign_identity="-",
        entitlements_file=None,
        icon=icns,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="TABifier",
    )
    app = BUNDLE(
        coll,
        name="TABifier.app",
        icon=icns,
        bundle_identifier="org.tabifier.app",
        version="1.04",
        info_plist={
            "CFBundleName": "TABifier",
            "CFBundleDisplayName": "TABifier",
            "CFBundleShortVersionString": "1.04",
            "CFBundleVersion": "1.04",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.music",
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="TABifier",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=["tabifier.ico"],
        version="version_info.txt",
    )
