"""
PyInstaller spec：打包 nursing-mm-qbank GUI（含 configs/，可离线运行 GUI 外壳）。

构建：
  pyinstaller --noconfirm --clean packaging/mm-qbank-gui.spec

产物：
  dist/mm-qbank-gui/mm-qbank-gui.exe   （onedir）

说明：
- 本项目运行时会读取「exe 同目录」下的 configs/default.yaml 与 .env
- 因此 spec 会把仓库的 configs/ 复制到 dist/mm-qbank-gui/configs/
"""

from __future__ import annotations

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = Path(globals().get("SPECPATH", ".")).resolve()
PROJ = SPEC_DIR.parent
SRC = PROJ / "src"

hiddenimports = []
hiddenimports += collect_submodules("mm_qbank")

a = Analysis(
    [str(SPEC_DIR / "entry_gui.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[
        (str(PROJ / "configs"), "configs"),
        (str(PROJ / ".env.example"), "."),  # 方便用户拷贝，不含真实密钥
        (str(PROJ / "README.md"), "."),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mm-qbank-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # GUI：无控制台窗口
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
    upx=True,
    upx_exclude=[],
    name="mm-qbank-gui",
)

