from __future__ import annotations

import re
import uuid
from pathlib import Path


def list_input_images(input_dir: Path) -> list[Path]:
    exts = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
    out: list[Path] = []
    for p in sorted(input_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in exts:
            out.append(p)
    return sorted(out)


def page_id_for(path: Path, root: Path | None = None) -> str:
    """
    用于输出文件名与 manifest 的 ``page_id``。

    若提供 ``root``（通常为输入图片根目录），则用**相对路径**（去扩展名、分隔符改为 ``__``）
    参与命名，避免不同子目录下**同名文件**互相覆盖导致「只剩一页或内容错乱」。
    未提供 ``root`` 时仍仅用 ``stem``（与旧行为兼容）。
    """
    stem: str
    if root is not None:
        try:
            rel = path.resolve().relative_to(root.resolve())
            stem = rel.with_suffix("").as_posix().replace("/", "__").replace("\\", "__")
        except ValueError:
            stem = path.stem
    else:
        stem = path.stem
    safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", stem)
    return safe[:120] or uuid.uuid4().hex[:12]
