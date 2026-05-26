"""
检测图像中面积最大的凸四边形（透视下常为梯形），估计「单据应占的」轴对齐矩形宽高，
再用同一单应性变换对**整张原图**做 ``warpPerspective``（扩展画布以包住变换后的四角），
而不是只裁出单据小图。
失败时由调用方决定是否回退到 :mod:`geometry` 的 deskew。
"""

from __future__ import annotations

import math
from typing import Any

import cv2
import numpy as np


def order_quad_points(pts: np.ndarray) -> np.ndarray:
    """四顶点排序为 [左上, 右上, 右下, 左下]，形状 (4, 2) float32。"""
    pts = np.asarray(pts, dtype=np.float32).reshape(-1, 2)
    if pts.shape[0] != 4:
        raise ValueError("需要恰好 4 个点")
    x_sorted = pts[np.argsort(pts[:, 0]), :]
    left = x_sorted[:2, :]
    right = x_sorted[2:, :]
    left = left[np.argsort(left[:, 1]), :]
    right = right[np.argsort(right[:, 1]), :]
    tl, bl = left
    tr, br = right
    return np.array([tl, tr, br, bl], dtype=np.float32)


def document_rect_dimensions_from_quad(quad_xy: np.ndarray) -> tuple[int, int]:
    """由四边形边长估算单据矫正后的轴对齐矩形宽高 (w, h)，至少为 2。"""
    rect = order_quad_points(quad_xy)
    tl, tr, br, bl = rect
    wa = float(np.linalg.norm(br - bl))
    wb = float(np.linalg.norm(tr - tl))
    max_w = max(int(round(wa)), int(round(wb)))
    ha = float(np.linalg.norm(tr - br))
    hb = float(np.linalg.norm(tl - bl))
    max_h = max(int(round(ha)), int(round(hb)))
    return max(max_w, 2), max(max_h, 2)


def warp_quad_to_rectangle(img_bgr: np.ndarray, quad_xy: np.ndarray) -> np.ndarray:
    """仅将四边形区域透视贴到矩形画布（裁切式）。整图矫正请用 :func:`warp_full_image_by_document_homography`。"""
    rect = order_quad_points(quad_xy)
    max_w, max_h = document_rect_dimensions_from_quad(quad_xy)
    dst = np.array(
        [[0, 0], [max_w - 1, 0], [max_w - 1, max_h - 1], [0, max_h - 1]],
        dtype=np.float32,
    )
    m = cv2.getPerspectiveTransform(rect, dst)
    return cv2.warpPerspective(
        img_bgr,
        m,
        (max_w, max_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )


def warp_full_image_by_document_homography(
    img_bgr: np.ndarray,
    src_quad_xy: np.ndarray,
    *,
    dst_width: int,
    dst_height: int,
    pad: int = 2,
) -> tuple[np.ndarray, dict[str, Any]]:
    """
    用 ``getPerspectiveTransform(单据四角 → 轴对齐矩形)`` 的同一矩阵，对**整幅** ``img_bgr`` 透视变换；
    输出尺寸为变换后原图四角包围盒（加 ``pad``），单据四边形在输出中落在 ``(0,0)-(dst_w-1,dst_h-1)`` 对应矩形处。
    """
    rect = order_quad_points(src_quad_xy)
    dst = np.array(
        [
            [0.0, 0.0],
            [float(dst_width - 1), 0.0],
            [float(dst_width - 1), float(dst_height - 1)],
            [0.0, float(dst_height - 1)],
        ],
        dtype=np.float32,
    )
    m = cv2.getPerspectiveTransform(rect, dst)
    h0, w0 = img_bgr.shape[:2]
    corners = np.array(
        [
            [[0.0, 0.0]],
            [[float(w0 - 1), 0.0]],
            [[float(w0 - 1), float(h0 - 1)]],
            [[0.0, float(h0 - 1)]],
        ],
        dtype=np.float32,
    )
    mapped = cv2.perspectiveTransform(corners, m).reshape(-1, 2)
    xmin = float(np.floor(np.min(mapped[:, 0]))) - float(pad)
    ymin = float(np.floor(np.min(mapped[:, 1]))) - float(pad)
    xmax = float(np.ceil(np.max(mapped[:, 0]))) + float(pad)
    ymax = float(np.ceil(np.max(mapped[:, 1]))) + float(pad)
    out_w = max(2, int(xmax - xmin))
    out_h = max(2, int(ymax - ymin))
    t = np.array([[1.0, 0.0, -xmin], [0.0, 1.0, -ymin], [0.0, 0.0, 1.0]], dtype=np.float64)
    m2 = (t @ m).astype(np.float64)
    out = cv2.warpPerspective(
        img_bgr,
        m2,
        (out_w, out_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )
    wmeta: dict[str, Any] = {
        "out_wh": (out_w, out_h),
        "dst_doc_wh": (dst_width, dst_height),
        "bbox_xyxy": (xmin, ymin, xmax, ymax),
        "H": m2.astype(np.float64).tolist(),
    }
    return out, wmeta


def crop_warped_to_expanded_quad_bbox(
    warped_bgr: np.ndarray,
    src_quad_xy: np.ndarray,
    homography_m2: np.ndarray,
    *,
    area_expand_ratio: float = 0.0,
) -> tuple[np.ndarray, dict[str, Any]]:
    """
    将原图四边形经 ``homography_m2`` 映射到 warped 后的位置，取轴对齐包围盒；
    面积按 ``(1 + area_expand_ratio)`` 以中心同比放大（宽高乘 ``sqrt(1+ratio)``），再裁切到图像范围内。
    ``area_expand_ratio <= 0`` 时仅裁紧贴包围盒（数值误差可略扩 1px）。
    """
    h, w = warped_bgr.shape[:2]
    rect = order_quad_points(np.asarray(src_quad_xy, dtype=np.float32))
    pts = rect.reshape(1, 4, 2).astype(np.float32)
    h32 = np.asarray(homography_m2, dtype=np.float32)
    mapped = cv2.perspectiveTransform(pts, h32).reshape(-1, 2)
    x_min = float(np.min(mapped[:, 0]))
    x_max = float(np.max(mapped[:, 0]))
    y_min = float(np.min(mapped[:, 1]))
    y_max = float(np.max(mapped[:, 1]))
    bw = max(x_max - x_min, 1.0)
    bh = max(y_max - y_min, 1.0)
    cx = (x_min + x_max) * 0.5
    cy = (y_min + y_max) * 0.5
    scale = math.sqrt(max(0.0, 1.0 + float(area_expand_ratio)))
    nw = bw * scale
    nh = bh * scale
    nx0 = int(math.floor(cx - nw * 0.5))
    ny0 = int(math.floor(cy - nh * 0.5))
    nx1 = int(math.ceil(cx + nw * 0.5))
    ny1 = int(math.ceil(cy + nh * 0.5))
    nx0 = max(0, nx0)
    ny0 = max(0, ny0)
    nx1 = min(w, nx1)
    ny1 = min(h, ny1)
    cmeta: dict[str, Any] = {
        "crop_xyxy": (nx0, ny0, nx1, ny1),
        "tight_xyxy": (
            max(0, int(math.floor(x_min))),
            max(0, int(math.floor(y_min))),
            min(w, int(math.ceil(x_max))),
            min(h, int(math.ceil(y_max))),
        ),
        "area_expand_ratio": float(area_expand_ratio),
    }
    if nx1 <= nx0 or ny1 <= ny0:
        cmeta["skipped"] = True
        return warped_bgr, cmeta
    crop = warped_bgr[ny0:ny1, nx0:nx1].copy()
    return crop, cmeta


def _scale_points_back(pts: np.ndarray, sx: float, sy: float) -> np.ndarray:
    out = pts.astype(np.float64).copy()
    out[:, 0] *= sx
    out[:, 1] *= sy
    return out.astype(np.float32)


def _find_quad_on_gray(
    gray_small: np.ndarray,
    *,
    min_area_ratio: float,
    epsilon_ratios: tuple[float, ...],
) -> np.ndarray | None:
    h, w = gray_small.shape[:2]
    area_img = float(h * w)
    blurred = cv2.GaussianBlur(gray_small, (5, 5), 0)
    edged = cv2.Canny(blurred, 40, 120)
    edged = cv2.dilate(edged, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=2)

    cnts, _ = cv2.findContours(edged, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cnts = sorted(cnts, key=cv2.contourArea, reverse=True)[:40]

    def try_contours(contours: list[np.ndarray]) -> np.ndarray | None:
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < min_area_ratio * area_img:
                continue
            peri = cv2.arcLength(cnt, True)
            if peri < 1e-6:
                continue
            for er in epsilon_ratios:
                approx = cv2.approxPolyDP(cnt, er * peri, True)
                if len(approx) == 4 and cv2.isContourConvex(approx):
                    return approx.reshape(4, 2).astype(np.float32)
        return None

    q = try_contours(cnts)
    if q is not None:
        return q

    # 备选：自适应二值 + 轮廓（适合边缘较弱的手写纸张）
    th = cv2.adaptiveThreshold(
        blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 25, 11
    )
    th = cv2.medianBlur(th, 5)
    cnts2, _ = cv2.findContours(th, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cnts2 = sorted(cnts2, key=cv2.contourArea, reverse=True)[:40]
    return try_contours(cnts2)


def rectify_largest_quad_to_rectangle(
    img_bgr: np.ndarray,
    *,
    min_area_ratio: float = 0.08,
    epsilon_ratios: tuple[float, ...] = (0.02, 0.03, 0.045, 0.06),
    max_detect_long_edge: int = 1200,
    rect_area_expand_ratio: float = 0.2,
) -> tuple[np.ndarray, dict[str, Any]]:
    """
    试验版透视矫正（按用户给定思路）：

    1) 灰度 + Canny
    2) findContours（仅外轮廓）
    3) approxPolyDP 找四边形（优先面积大的）
    4) 将四角映射到固定画布（默认 800x1100）并 warpPerspective

    注：该实现不会再做“整图扩展画布 + 裁切单据”那套逻辑，而是直接输出固定尺寸的矫正图。
    """
    _ = max_detect_long_edge, rect_area_expand_ratio  # 保留签名兼容，但此试验实现不使用

    meta: dict[str, Any] = {"applied": False, "reason": None}
    h0, w0 = img_bgr.shape[:2]
    if h0 < 10 or w0 < 10:
        meta["reason"] = "image_too_small"
        return img_bgr, meta

    def _quad_shape_score(quad_xy: np.ndarray) -> float:
        """
        衡量四边形“像不像矩形”：边长越均衡越好。
        返回 (0, 1]，越接近 1 越像矩形。
        """
        pts = order_quad_points(quad_xy)
        tl, tr, br, bl = pts
        edges = [
            float(np.linalg.norm(tl - tr)),
            float(np.linalg.norm(tr - br)),
            float(np.linalg.norm(br - bl)),
            float(np.linalg.norm(bl - tl)),
        ]
        mn = max(min(edges), 1e-6)
        mx = max(edges)
        ratio = mx / mn
        return 1.0 / ratio

    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    # Upgrade 2：双通道 Canny 融合（低阈值保召回，高阈值保干净）
    # 阈值偏高：减少噪声边缘与误检
    edges1 = cv2.Canny(blurred, 30, 90)
    edges2 = cv2.Canny(blurred, 70, 160)
    edges = cv2.bitwise_or(edges1, edges2)

    # 进阶优化：形态学闭运算连接边缘（比单纯 dilate 更安全）
    edges = cv2.morphologyEx(
        edges,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        iterations=1,
    )
    cnts, _hier = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        meta["reason"] = "no_contours"
        return img_bgr, meta

    area_img = float(h0 * w0)
    cnts = sorted(cnts, key=cv2.contourArea, reverse=True)

    # Upgrade 1：评分机制（不再 accept first quad）
    best_score = -1.0
    best_quad: np.ndarray | None = None
    best_meta: dict[str, Any] = {}

    def _maybe_update(quad_xy: np.ndarray, *, area: float, source: str, er: float | None = None) -> None:
        nonlocal best_score, best_quad, best_meta
        quad_xy = quad_xy.reshape(4, 2).astype(np.float32)
        if not cv2.isContourConvex(quad_xy.reshape(-1, 1, 2)):
            return
        # 进阶优化：边界过滤（宽高比极端的直接拒绝）
        x, y, w, h = cv2.boundingRect(quad_xy.reshape(-1, 1, 2).astype(np.int32))
        if w <= 0 or h <= 0:
            return
        aspect = float(w) / float(h)
        if aspect < 0.2 or aspect > 5.0:
            return
        shape_score = _quad_shape_score(quad_xy)
        final_score = float(area) * float(shape_score)
        if final_score > best_score:
            best_score = final_score
            best_quad = quad_xy
            best_meta = {
                "source": source,
                "score": final_score,
                "shape_score": shape_score,
                "bbox_aspect": aspect,
            }
            if er is not None:
                best_meta["epsilon_ratio"] = float(er)

    # 先尝试所有轮廓的四边形近似
    for cnt in cnts[:120]:
        area = float(cv2.contourArea(cnt))
        if area < float(min_area_ratio) * area_img:
            continue
        peri = cv2.arcLength(cnt, True)
        if peri < 1e-6:
            continue
        for er in (epsilon_ratios or (0.02, 0.03, 0.045, 0.06)):
            approx = cv2.approxPolyDP(cnt, float(er) * peri, True)
            if len(approx) == 4:
                _maybe_update(approx.reshape(4, 2), area=area, source="approx", er=float(er))

    # Upgrade 3：凸包兜底机制（取最大轮廓的凸包再近似）
    if best_quad is None and cnts:
        largest = cnts[0]
        area = float(cv2.contourArea(largest))
        if area >= float(min_area_ratio) * area_img:
            peri = cv2.arcLength(largest, True)
            hull = cv2.convexHull(largest)
            peri_h = cv2.arcLength(hull, True)
            # 在 hull 上再做多 epsilon 近似
            for er in (epsilon_ratios or (0.02, 0.03, 0.045, 0.06)):
                approx = cv2.approxPolyDP(hull, float(er) * (peri_h if peri_h > 1e-6 else peri), True)
                if len(approx) == 4:
                    _maybe_update(approx.reshape(4, 2), area=area, source="hull", er=float(er))

    quad_full = best_quad
    if quad_full is None:
        meta["reason"] = "no_quad_found"
        return img_bgr, meta

    meta["quad_area_ratio"] = float(cv2.contourArea(quad_full.reshape(-1, 1, 2))) / area_img
    meta["quad_xy"] = quad_full.tolist()
    meta["quad_pick"] = best_meta

    # 方案 A：根据四边形边长估算目标画布宽高，避免强行挤压到固定竖版画布
    dst_w, dst_h = document_rect_dimensions_from_quad(quad_full)
    src_pts = order_quad_points(quad_full)
    dst_pts = np.array(
        [
            [0.0, 0.0],
            [float(dst_w - 1), 0.0],
            [float(dst_w - 1), float(dst_h - 1)],
            [0.0, float(dst_h - 1)],
        ],
        dtype=np.float32,
    )

    m = cv2.getPerspectiveTransform(src_pts, dst_pts)
    corrected = cv2.warpPerspective(
        img_bgr,
        m,
        (dst_w, dst_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )
    meta["dst_doc_wh"] = (dst_w, dst_h)
    meta["out_wh"] = (dst_w, dst_h)
    meta["applied"] = True
    meta["reason"] = "perspective_ok_simple"
    return corrected, meta


def perspective_options_from_config(raw: dict[str, Any] | None) -> dict[str, Any]:
    """解析 ``configs.default.yaml`` 中 ``preprocess.perspective``。"""
    if not raw or not isinstance(raw, dict):
        return {"enabled": False}
    if not bool(raw.get("enabled", False)):
        return {"enabled": False}
    eps = raw.get("approx_epsilon_ratios")
    if isinstance(eps, list) and eps:
        er = tuple(float(x) for x in eps)
    else:
        er = (0.02, 0.03, 0.045, 0.06)
    return {
        "enabled": True,
        "min_area_ratio": float(raw.get("min_area_ratio", 0.08)),
        "epsilon_ratios": er,
        "max_detect_long_edge": int(raw.get("max_detect_long_edge", 1200)),
        "rect_area_expand_ratio": float(raw.get("rect_area_expand_ratio", 0.2)),
    }
