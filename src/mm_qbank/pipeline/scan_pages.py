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


def page_id_for(path: Path) -> str:
    safe = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", path.stem)
    return safe[:80] or uuid.uuid4().hex[:12]
