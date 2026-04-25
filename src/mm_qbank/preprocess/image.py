from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

try:
    from PIL import Image, ImageOps
except Exception:  # noqa: BLE001
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]


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


def _rotate_90_bgr(img: np.ndarray, k: int) -> np.ndarray:
    """按 90 度倍数旋转：k=0/1/2/3 -> 0/90/180/270（顺时针）。"""
    kk = int(k) % 4
    if kk == 0:
        return img
    if kk == 1:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if kk == 2:
        return cv2.rotate(img, cv2.ROTATE_180)
    return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)


def _orientation_score(gray: np.ndarray) -> float:
    """
    评估“文本横排”倾向：横向长线越多、纵向长线越少 -> 分数越高。
    只做启发式判断（不调用 OCR），足够用于 0/90/180/270 的自动旋转。
    """
    h, w = gray.shape[:2]
    if min(h, w) < 80:
        return 0.0
    work = gray
    m = max(h, w)
    if m > 1200:
        scale = 1200.0 / float(m)
        work = cv2.resize(work, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    work = cv2.GaussianBlur(work, (3, 3), 0)
    edges = cv2.Canny(work, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180.0,
        threshold=90,
        minLineLength=max(40, int(min(work.shape) * 0.08)),
        maxLineGap=8,
    )
    if lines is None:
        return 0.0
    horiz = 0.0
    vert = 0.0
    for arr in lines[:800]:
        x1, y1, x2, y2 = [int(v) for v in arr[0]]
        dx = float(x2 - x1)
        dy = float(y2 - y1)
        length = float((dx * dx + dy * dy) ** 0.5)
        if length < 1.0:
            continue
        ang = abs(float(np.degrees(np.arctan2(dy, dx))))
        # ang in [0, 180]
        if ang > 90.0:
            ang = 180.0 - ang
        if ang <= 15.0:
            horiz += length
        elif ang >= 75.0:
            vert += length
    # 加一点边缘密度让“无直线”场景也不至于全相等
    dens = float(np.count_nonzero(edges)) / float(edges.size)
    return float(horiz - 1.15 * vert + dens * 50.0)


def _auto_rotate_upright(img: np.ndarray) -> tuple[np.ndarray, int]:
    """在 0/90/180/270 中挑选最像横排文本的方向。返回 (img, k)。"""
    best_k = 0
    best_s = -1e30
    for k in (0, 1, 2, 3):
        cand = _rotate_90_bgr(img, k)
        gray = cv2.cvtColor(cand, cv2.COLOR_BGR2GRAY)
        s = _orientation_score(gray)
        if s > best_s:
            best_s = s
            best_k = k
    return _rotate_90_bgr(img, best_k), int(best_k)


def preprocess_image(
    src: Path,
    dst: Path,
    max_long_edge: int = 2000,
    deskew: dict[str, Any] | bool | None = None,
    tone_mode: str = "shaded",
    rotate_degrees: float = 0.0,
    auto_exif: bool = True,
    auto_rotate: bool = False,
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
    # 先尝试按 EXIF 方向纠正（手机竖拍常见），再转回 OpenCV BGR
    img: np.ndarray | None = None
    if auto_exif and Image is not None and ImageOps is not None:
        try:
            with Image.open(src) as im:
                im2 = ImageOps.exif_transpose(im)
                arr = np.array(im2.convert("RGB"))
                img = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        except Exception:  # noqa: BLE001
            img = None
    if img is None:
        img = cv2.imdecode(np.fromfile(str(src), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"无法读取图像: {src}")

    # 自动判断 0/90/180/270（在任何 deskew/固定旋转前）
    if bool(auto_rotate):
        try:
            img, k = _auto_rotate_upright(img)
        except Exception:  # noqa: BLE001
            k = 0

    # 固定旋转（例如 90/180/270 或任意角度），在 deskew 前做
    if abs(float(rotate_degrees)) > 1e-6:
        img = _rotate_bound_bgr(img, float(rotate_degrees))
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
