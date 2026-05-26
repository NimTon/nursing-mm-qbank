"""几何预处理：透视矫正（见 ``perspective_rectify``）。"""

from __future__ import annotations

from typing import Any

import numpy as np

from mm_qbank.preprocess.perspective_rectify import (
    perspective_options_from_config,
    rectify_largest_quad_to_rectangle,
)


def apply_perspective_bgr(
    img_bgr: np.ndarray,
    perspective: dict[str, Any] | bool | None,
) -> tuple[np.ndarray, dict[str, Any]]:
    """按配置对 BGR 图做透视矫正；失败或未启用时返回原图。"""
    if perspective is True:
        raw: dict[str, Any] | None = {"enabled": True}
    elif isinstance(perspective, dict):
        raw = perspective
    else:
        raw = None
    popts = perspective_options_from_config(raw)
    if not popts.get("enabled"):
        return img_bgr, {"applied": False, "reason": "disabled"}
    try:
        return rectify_largest_quad_to_rectangle(
            img_bgr,
            min_area_ratio=float(popts["min_area_ratio"]),
            epsilon_ratios=tuple(popts["epsilon_ratios"]),
            max_detect_long_edge=int(popts["max_detect_long_edge"]),
            rect_area_expand_ratio=float(popts["rect_area_expand_ratio"]),
        )
    except Exception as e:  # noqa: BLE001
        return img_bgr, {"applied": False, "reason": "error", "error": f"{type(e).__name__}: {e}"}


def perspective_from_config_block(cfg: dict[str, Any] | None, key: str = "perspective") -> dict[str, Any] | None:
    if not cfg:
        return None
    raw = cfg.get(key)
    return raw if isinstance(raw, dict) else None
