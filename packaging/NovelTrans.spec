# PyInstaller spec for NovelTrans (macOS .app). Build: make app
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = collect_data_files("noveltrans")  # translators/data/*.json
binaries = []
hiddenimports = []

# Packages that ship data files / dynamic submodules PyInstaller can miss.
for pkg in ("vieneu", "onnxruntime", "jieba", "opencc", "ebooklib"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="NovelTrans",
)
app = BUNDLE(
    coll,
    name="NovelTrans.app",
    icon="NovelTrans.icns",
    bundle_identifier="cloud.datastack.noveltrans",
    info_plist={
        "CFBundleName": "NovelTrans",
        "CFBundleDisplayName": "NovelTrans",
        "CFBundleShortVersionString": "0.1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
