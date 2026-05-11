# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path


ROOT = Path.cwd()
RESOURCES = ROOT / "Lecture Slide Capture.app" / "Contents" / "Resources"
ICON = RESOURCES / "AppIcon.ico"


a = Analysis(
    [str(RESOURCES / "slide_capture_gui.py")],
    pathex=[str(RESOURCES)],
    binaries=[],
    datas=[
        (str(RESOURCES / "requirements.txt"), "."),
        (str(ICON), "."),
    ],
    hiddenimports=[
        "slide_capture",
        "PIL.Image",
        "PIL.ImageDraw",
        "PIL.ImageTk",
        "PIL._tkinter_finder",
        "skimage.metrics",
        "skimage.metrics._structural_similarity",
        "mss.windows",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "AppKit",
        "Foundation",
        "Quartz",
        "ScreenCaptureKit",
    ],
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
    name="LectureSlideCapture",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON),
)
