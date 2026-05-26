from __future__ import annotations

import cv2
import numpy as np

from mm_qbank.preprocess.geometry import apply_perspective_bgr
from mm_qbank.preprocess.perspective_rectify import (
    order_quad_points,
    perspective_options_from_config,
    rectify_largest_quad_to_rectangle,
)


def test_perspective_options_from_config() -> None:
    assert perspective_options_from_config(None)["enabled"] is False
    o = perspective_options_from_config({"enabled": True, "min_area_ratio": 0.1})
    assert o["enabled"] is True
    assert o["min_area_ratio"] == 0.1


def test_rectify_finds_document_quad_synthetic() -> None:
    h, w = 400, 600
    img = np.full((h, w, 3), 255, dtype=np.uint8)
    cv2.rectangle(img, (40, 40), (w - 40, h - 40), (0, 0, 0), 8)
    out, meta = rectify_largest_quad_to_rectangle(
        img,
        min_area_ratio=0.15,
        epsilon_ratios=(0.02, 0.03, 0.05, 0.08),
        max_detect_long_edge=800,
    )
    assert meta.get("applied") is True
    cw, ch = meta["out_wh"]
    assert cw >= 80 and ch >= 80
    assert out.shape[1] == cw and out.shape[0] == ch


def test_apply_perspective_disabled_passthrough() -> None:
    img = np.ones((80, 120, 3), dtype=np.uint8) * 200
    out, meta = apply_perspective_bgr(img, {"enabled": False})
    assert meta.get("applied") is False
    assert out.shape == img.shape


def test_order_quad_points() -> None:
    pts = np.array([[10.0, 20.0], [100.0, 15.0], [95.0, 80.0], [5.0, 75.0]], dtype=np.float32)
    o = order_quad_points(pts)
    assert o.shape == (4, 2)
    assert o[0][1] <= o[3][1] + 1e-3
