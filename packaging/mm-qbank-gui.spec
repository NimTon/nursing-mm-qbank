"""
PyInstaller spec：打包 nursing-mm-qbank GUI（含 configs/，可离线运行 GUI 外壳）。

构建：
  pyinstaller --noconfirm --clean packaging/mm-qbank-gui.spec

产物：
  dist/mm-qbank-gui/mm-qbank-gui.exe   （onedir）

说明：
- 本项目运行时会读取「exe 同目录」下的 configs/、models/ 与 .env（见 config.project_root）
- PyInstaller 6 onedir 会把 datas 放进 _internal/，不符合「旁置可改」的预期
- configs/、models/、.env 由 build.ps1 在打包完成后复制到 dist/mm-qbank-gui/ 根目录
"""

from __future__ import annotations

from pathlib import Path

import sys

from PyInstaller.utils.hooks import collect_submodules

SPEC_DIR = Path(globals().get("SPECPATH", ".")).resolve()
PROJ = SPEC_DIR.parent
SRC = PROJ / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

hiddenimports = []
_mm_subs = collect_submodules("mm_qbank")
if not _mm_subs:
    raise RuntimeError(
        "未能收集 mm_qbank 子模块。请先 pip install -e . ，并确认 "
        'python -c "import mm_qbank" 成功（若 __init__.py 含空字节会失败）。'
    )
hiddenimports += _mm_subs
for _pkg in ("paddle", "paddle.inference", "cv2", "docx"):
    try:
        hiddenimports += collect_submodules(_pkg)
    except Exception:
        hiddenimports.append(_pkg)

# 用户可编辑资源不打进 _internal；build.ps1 复制到 exe 同目录
_datas: list[tuple[str, str]] = []

a = Analysis(
    [str(SPEC_DIR / "entry_gui.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=_datas,
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

