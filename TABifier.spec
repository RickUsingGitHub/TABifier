# -*- mode: python ; coding: utf-8 -*-
"""One-file freeze of TABifier. Rebuild with:  python -m PyInstaller --noconfirm --clean TABifier.spec"""

from PyInstaller.utils.hooks import collect_all

datas = [
    ("kofi_qr.png", "."),
    ("kofi_button.png", "."),
    ("settings.json", "."),
]
binaries = []
hiddenimports = [
    "windnd",
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

for pkg in ("tkinterdnd2", "pdfplumber", "pypdfium2", "pypdfium2_raw"):
    d, b, h = collect_all(pkg)
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
