# -*- mode: python ; coding: utf-8 -*-
"""Windows executable for the Konsil form app."""

from PyInstaller.utils.hooks import collect_all

pdfium_datas, pdfium_binaries, pdfium_hidden = collect_all("pypdfium2")
raw_datas, raw_binaries, raw_hidden = collect_all("pypdfium2_raw")

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=pdfium_binaries + raw_binaries,
    datas=[("Konsil_Formular_empty.pdf", ".")] + pdfium_datas + raw_datas,
    hiddenimports=[
        "win32print",
        "win32ui",
        "win32con",
        "win32timezone",
        "pythoncom",
        "pywintypes",
    ]
    + pdfium_hidden
    + raw_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Konsil",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
