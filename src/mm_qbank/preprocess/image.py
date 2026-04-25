from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np


def _deskew_params(deskew: dict[str, Any] | bool | None) -> tuple[bool, float, float]:
    if deskew is None or deskew is False:
        return False, 20.0, 0.35
    if deskew is True:
        return True, 20.0, 0.35
    if isinstance(deskew, dict):
        if not bool(deskew.get("enabled", False)):
            return False, 20.0, 0.35
        return (
            True,
            float(deskew.get("max_abs_degrees", 20.0)),
            float(deskew.get("min_abs_degrees", 0.35)),
        )
    return False, 20.0, 0.35


def _estimate_skew_angle_deg(gray: np.ndarray, *, max_abs_deg: float) -> float:
    """用 Hough 检测近似水平笔画/行线，取倾斜角中位数（适合书页、拍题小角度）。"""
    h, w = gray.shape[:2]
    if min(h, w) < 48:
        return 0.0
    work = gray
    scale = 1.0
    m = max(h, w)
    if m > 1400:
        scale = 1400.0 / m
        work = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    blur = cv2.GaussianBlur(work, (3, 3), 0)
    edges = cv2.Canny(blur, 50, 150, apertureSize=3)
    min_votes = max(int(min(work.shape) * 0.28), 80)
    lines = cv2.HoughLines(edges, rho=1, theta=np.pi / 180.0, threshold=min_votes)
    if lines is None:
        return 0.0
    candidates: list[float] = []
    for arr in lines[:400]:
        theta = float(arr[0][1])
        deg_theta = float(np.degrees(theta))
        # 近似水平线段：法向接近竖直 -> theta 接近 90°
        skew = 90.0 - deg_theta
        while skew > 45.0:
            skew -= 180.0
        while skew < -45.0:
            skew += 180.0
        if abs(skew) <= max_abs_deg:
            candidates.append(skew)
    if len(candidates) < 3:
        return 0.0
    return float(np.median(candidates))


def _rotate_bound_bgr(img: np.ndarray, angle_deg: float) -> np.ndarray:
    if abs(angle_deg) < 1e-6:
        return img
    h, w = img.shape[:2]
    center = (w / 2.0, h / 2.0)
    m2d = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    cos = abs(m2d[0, 0])
    sin = abs(m2d[0, 1])
    n_w = int(round(h * sin + w * cos))
    n_h = int(round(h * cos + w * sin))
    m2d[0, 2] += n_w / 2.0 - center[0]
    m2d[1, 2] += n_h / 2.0 - center[1]
    return cv2.warpAffine(
        img,
        m2d,
        (n_w, n_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )


def preprocess_image(
    src: Path,
    dst: Path,
    max_long_edge: int = 2000,
    deskew: dict[str, Any] | bool | None = None,
    tone_mode: str = "shaded",
) -> Path:
    """
    ``max_long_edge``:
    - 正数：长边超过该值时等比缩小（减轻显存/耗时）；默认 2000。
    - 0 或负数：不缩小，保留原图尺寸供多模态读图（无损编码仍以 PNG 写盘）。

    ``tone_mode``:
    - ``shaded``（默认）：高斯平滑 + 大核估计照度再 ``divide``，削弱阴影、利于文字识别，但观感偏「糊/平」。
    - ``raw``：只做缩放与可选纠偏，输出彩色图，更接近原图清晰度（逆光/阴影重时识别可能变差）。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    img = cv2.imdecode(np.fromfile(str(src), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"无法读取图像: {src}")
    h, w = img.shape[:2]
    long = max(h, w)
    # max_long_edge <= 0：不缩小，按原图分辨率给 VLM（更大显存/更慢，PNG 仍无损写盘）
    if max_long_edge > 0 and long > max_long_edge:
        scale = max_long_edge / float(long)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    on, max_abs, min_abs = _deskew_params(deskew)
    if on:
        gray_for_angle = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        ang = _estimate_skew_angle_deg(gray_for_angle, max_abs_deg=max_abs)
        if abs(ang) >= min_abs:
            img = _rotate_bound_bgr(img, ang)

    mode = str(tone_mode or "shaded").lower().strip()
    if mode == "raw":
        out = img
    else:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (0, 0), 2)
        shade = cv2.GaussianBlur(gray, (0, 0), 35)
        norm = cv2.divide(gray, shade, scale=255)
        out = cv2.cvtColor(norm, cv2.COLOR_GRAY2BGR)
    ok, buf = cv2.imencode(".png", out)
    if not ok:
        raise RuntimeError("PNG 编码失败")
    buf.tofile(str(dst))
    return dst
