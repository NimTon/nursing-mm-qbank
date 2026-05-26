"""扫描流水线共用工具（暂停/取消、并行度、图片 MIME）。"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable


def wait_unpaused(
    *,
    pause: Callable[[], bool] | None,
    cancel: threading.Event | None,
    poll_s: float = 0.2,
) -> bool:
    """若已取消或暂停后用户结束，返回 True。"""
    while True:
        if cancel is not None and cancel.is_set():
            return True
        if pause is None or not pause():
            return False
        time.sleep(poll_s)


def cap_workers(n_tasks: int, raw: object) -> int:
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        w = min(8, (os.cpu_count() or 4) * 2)
    else:
        w = int(raw)
    w = max(1, w)
    return min(w, max(1, n_tasks))


def content_type(suffix: str) -> str:
    s = suffix.lower()
    if s == ".png":
        return "image/png"
    if s in (".jpg", ".jpeg"):
        return "image/jpeg"
    if s == ".webp":
        return "image/webp"
    if s == ".bmp":
        return "image/bmp"
    return "application/octet-stream"
