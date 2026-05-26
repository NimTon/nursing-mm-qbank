"""透视/合并调试可视化。"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from mm_qbank.preprocess.perspective_rectify import order_quad_points


def draw_quad_on_bgr(
    img_bgr: np.ndarray,
    quad_xy: list | np.ndarray,
    *,
    color: tuple[int, int, int] = (0, 255, 0),
    thickness: int = 3,
    draw_vertices: bool = True,
    draw_labels: bool = True,
) -> np.ndarray:
    """在原图上绘制透视检测四边形（矫正框）。"""
    out = img_bgr.copy()
    pts = np.asarray(quad_xy, dtype=np.float32).reshape(4, 2)
    ordered = order_quad_points(pts)
    poly = ordered.astype(np.int32).reshape(-1, 1, 2)
    cv2.polylines(out, [poly], isClosed=True, color=color, thickness=thickness)
    if draw_vertices:
        for i, (x, y) in enumerate(ordered):
            cv2.circle(out, (int(x), int(y)), max(4, thickness + 2), color, -1)
            if draw_labels:
                labels = ("TL", "TR", "BR", "BL")
                cv2.putText(
                    out,
                    labels[i],
                    (int(x) + 6, int(y) - 6),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    color,
                    2,
                    cv2.LINE_AA,
                )
    return out


def annotate_status_bar(img_bgr: np.ndarray, lines: list[str]) -> np.ndarray:
    """左上角叠加状态文字（黑底绿字）。"""
    out = img_bgr.copy()
    y = 24
    for line in lines:
        cv2.putText(
            out,
            line,
            (12, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 0, 0),
            4,
            cv2.LINE_AA,
        )
        cv2.putText(
            out,
            line,
            (12, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )
        y += 22
    return out
