# PyInstaller spec for NovelTrans (Windows, onedir). Build: .\make.ps1 app
#
# Sibling to NovelTrans.spec rather than a platform branch of it: PyInstaller's BUNDLE()
# step (macOS .app bundle) is macOS-only and would error out if reached here, and there's
# no onefile/onedir equivalent decision to share — this stops at COLLECT() instead.
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = collect_data_files("noveltrans")  # translators/data/*.json
binaries = []
hiddenimports = []

# Packages that ship data files / dynamic submodules PyInstaller can miss. Keep this list
# in sync with NovelTrans.spec (macOS) — duplicated rather than shared/imported since it's
# short and rarely changes, and a spec file isn't a normal importable module.
for pkg in ("vieneu", "sea_g2p", "onnxruntime", "jieba", "opencc", "ebooklib"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Bundled ffmpeg/ffprobe — see README "Đóng gói thành app Windows" for how to fetch them
# into packaging/ffmpeg/win/ before building. Optional at build time: if absent, the exe
# still runs fine, falling back to whatever ffmpeg a user has installed themselves (found
# via PATH — see runtime_env.augment_tool_path's Windows candidate dirs), same as the
# unbundled macOS build already does.
_ffmpeg_dir = Path("ffmpeg") / "win"
for _name in ("ffmpeg.exe", "ffprobe.exe"):
    _bin = _ffmpeg_dir / _name
    if _bin.is_file():
        binaries.append((str(_bin), "."))
    else:
        print(f"[NovelTrans-windows.spec] {_bin} not found — building without bundled {_name}")

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NovelTrans",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI app: no terminal window
    icon="NovelTrans.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="NovelTrans",
)
