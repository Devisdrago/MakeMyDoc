# -*- mode: python ; coding: utf-8 -*-
# Build with:  pyinstaller MakeMyDoc.spec
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas, binaries, hiddenimports = [], [], [
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "pystray._win32",
    "PIL._tkinter_finder",
]
# uiautomation generates comtypes wrappers at runtime and ships helper files.
for pkg in ("uiautomation", "comtypes"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

datas += collect_data_files("sv_ttk")     # Windows 11 (Sun Valley) theme files

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MakeMyDoc",
    debug=False,
    strip=False,
    upx=False,
    console=False,          # windowed app, no console
    icon="assets/MakeMyDoc.ico",
    uac_admin=False,        # normal launch; use Tools > Restart as administrator when needed
)
