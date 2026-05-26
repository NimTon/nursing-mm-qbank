from __future__ import annotations

import numpy as np

from mm_qbank.preprocess.ocr_rotate import (
    auto_rotate_upright_pulc_bgr,
    ocr_rotate_options_from_config,
    rotate_90_bgr,
)


def test_ocr_rotate_options_from_config() -> None:
    assert ocr_rotate_options_from_config(False) == {"enabled": False}
    assert ocr_rotate_options_from_config(True)["enabled"] is True
    o = ocr_rotate_options_from_config({"enabled": True, "max_long_edge": 800})
    assert o == {"enabled": True, "max_long_edge": 800}


def test_rotate_90_bgr_roundtrip() -> None:
    img = np.zeros((40, 80, 3), dtype=np.uint8)
    img[:, :10] = 255
    out = rotate_90_bgr(rotate_90_bgr(img, 1), 3)
    assert out.shape == img.shape


def test_auto_rotate_pulc_without_paddle_returns_original() -> None:
    img = np.ones((120, 200, 3), dtype=np.uint8) * 128
    out, k, meta = auto_rotate_upright_pulc_bgr(img, max_long_edge=400)
    assert out.shape == img.shape
    assert k == 0
    assert isinstance(meta, dict)
