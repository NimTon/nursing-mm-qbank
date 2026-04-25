from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from mm_qbank.preprocess.image import preprocess_image


def test_preprocess_max_long_edge_zero_does_not_downscale(tmp_path: Path) -> None:
    src = tmp_path / "in.png"
    img = np.ones((1200, 3000, 3), dtype=np.uint8) * 200
    cv2.imwrite(str(src), img)
    dst = tmp_path / "out.png"
    preprocess_image(src, dst, max_long_edge=0, deskew=False)
    out = cv2.imread(str(dst))
    assert out is not None
    assert out.shape[0] == 1200 and out.shape[1] == 3000


def test_preprocess_image_writes_png_smaller_long_edge(tmp_path: Path) -> None:
    src = tmp_path / "in.png"
    img = np.ones((800, 400, 3), dtype=np.uint8) * 200
    cv2.imwrite(str(src), img)
    dst = tmp_path / "out.png"
    preprocess_image(src, dst, max_long_edge=300, deskew=False)
    assert dst.is_file()
    out = cv2.imread(str(dst))
    assert out is not None
    assert max(out.shape[0], out.shape[1]) <= 300


def test_preprocess_tone_mode_raw_keeps_color_channels(tmp_path: Path) -> None:
    src = tmp_path / "in.png"
    img = np.zeros((120, 160, 3), dtype=np.uint8)
    img[:, :, 1] = 200
    cv2.imwrite(str(src), img)
    dst = tmp_path / "out_raw.png"
    preprocess_image(src, dst, max_long_edge=500, deskew=False, tone_mode="raw")
    out = cv2.imread(str(dst))
    assert out is not None
    assert out.shape[2] == 3
    assert out[:, :, 1].mean() > out[:, :, 0].mean()


def test_preprocess_deskew_true_does_not_crash(tmp_path: Path) -> None:
    src = tmp_path / "in.png"
    img = np.ones((400, 300, 3), dtype=np.uint8) * 220
    cv2.rectangle(img, (20, 50), (280, 52), (0, 0, 0), -1)
    cv2.rectangle(img, (20, 120), (280, 122), (0, 0, 0), -1)
    cv2.imwrite(str(src), img)
    dst = tmp_path / "out.png"
    preprocess_image(src, dst, max_long_edge=600, deskew={"enabled": True})
    assert dst.is_file()
    out = cv2.imread(str(dst))
    assert out is not None
