from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from mm_qbank.preprocess.geometry import apply_perspective_bgr
from mm_qbank.preprocess.visualize import draw_quad_on_bgr


def test_draw_quad_on_synthetic(tmp_path: Path) -> None:
    h, w = 300, 400
    img = np.full((h, w, 3), 255, dtype=np.uint8)
    cv2.rectangle(img, (50, 40), (w - 50, h - 40), (0, 0, 0), 6)
    out, meta = apply_perspective_bgr(img, {"enabled": True, "min_area_ratio": 0.12})
    assert meta.get("applied") is True
    quad = meta["quad_xy"]
    vis = draw_quad_on_bgr(img, quad)
    dst = tmp_path / "quad_vis.png"
    assert cv2.imwrite(str(dst), vis)
    assert dst.stat().st_size > 100
