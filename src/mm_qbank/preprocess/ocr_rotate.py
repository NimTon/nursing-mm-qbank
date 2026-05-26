"""
文字方向检测（0/90/180/270°）并转正。

参考 delivery-slip-vlm：PaddleClas PULC ``text_image_orientation``。

- 配置 ``preprocess.auto_rotate_ocr``
- 需安装：``pip install paddlepaddle paddleclas``（未安装时保持原图）
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from mm_qbank.config import project_root

_PULC_CACHE: dict[str, Any] = {}
_PULC_PREDICT_LOCK = threading.Lock()


def rotate_90_bgr(img: np.ndarray, k: int) -> np.ndarray:
    """k=0/1/2/3 对应 0°/90°/180°/270°（顺时针 90° 为 1）。"""
    kk = int(k) % 4
    if kk == 0:
        return img
    if kk == 1:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if kk == 2:
        return cv2.rotate(img, cv2.ROTATE_180)
    return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)


def ocr_rotate_options_from_config(raw: Any) -> dict[str, Any]:
    """解析 ``auto_rotate_ocr`` 配置块。"""
    if raw is None or raw is False:
        return {"enabled": False}
    if raw is True:
        return {"enabled": True, "max_long_edge": 1200}
    if isinstance(raw, dict):
        if not bool(raw.get("enabled", False)):
            return {"enabled": False}
        le = raw.get("max_long_edge", 1200)
        return {
            "enabled": True,
            "max_long_edge": int(le) if le is not None else 1200,
        }
    return {"enabled": False}


class _PulcLegacyPredictor:
    """PaddleClas PULC 旧格式推理（兼容 Windows / Paddle 3.x）。"""

    def __init__(self, model_dir: str):
        from paddle.inference import Config, create_predictor  # type: ignore

        model_file = str(Path(model_dir) / "inference.pdmodel")
        params_file = str(Path(model_dir) / "inference.pdiparams")
        if not Path(model_file).is_file() or not Path(params_file).is_file():
            raise FileNotFoundError(f"missing model files in {model_dir}")

        cfg = Config(model_file, params_file)
        cfg.disable_gpu()
        cfg.disable_glog_info()
        cfg.switch_ir_optim(False)
        if hasattr(cfg, "disable_mkldnn"):
            try:
                cfg.disable_mkldnn()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        cfg.enable_memory_optim()
        cfg.switch_use_feed_fetch_ops(False)
        self.predictor = create_predictor(cfg)
        in_name = self.predictor.get_input_names()[0]
        out_name = self.predictor.get_output_names()[0]
        self.in_handle = self.predictor.get_input_handle(in_name)
        self.out_handle = self.predictor.get_output_handle(out_name)

    @staticmethod
    def _preprocess(img_bgr: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        if min(h, w) <= 0:
            raise ValueError("bad image")
        short = min(h, w)
        scale = 256.0 / float(short)
        nh, nw = int(round(h * scale)), int(round(w * scale))
        rgb = cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_LINEAR)
        ch, cw = 224, 224
        y0 = max((nh - ch) // 2, 0)
        x0 = max((nw - cw) // 2, 0)
        rgb = rgb[y0 : y0 + ch, x0 : x0 + cw]
        if rgb.shape[0] != ch or rgb.shape[1] != cw:
            rgb = cv2.resize(rgb, (cw, ch), interpolation=cv2.INTER_LINEAR)
        x = rgb.astype("float32") / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype="float32")
        std = np.array([0.229, 0.224, 0.225], dtype="float32")
        x = (x - mean) / std
        x = np.transpose(x, (2, 0, 1))
        return np.expand_dims(x, 0)

    def predict_k(self, img_bgr: np.ndarray) -> tuple[int, dict[str, Any]]:
        x = self._preprocess(img_bgr)
        with _PULC_PREDICT_LOCK:
            self.in_handle.copy_from_cpu(x)
            self.predictor.run()
            out = self.out_handle.copy_to_cpu()
        out = np.asarray(out)
        logits = out[0] if out.ndim == 2 and out.shape[0] == 1 else out.reshape(-1)
        idx = int(np.argmax(logits))
        pred_k = idx % 4
        return pred_k, {"legacy": True, "class_id": idx, "pred_k": pred_k}


def _resolve_text_image_orientation_model_dir() -> Path:
    p0 = (project_root() / "models" / "text_image_orientation").resolve()
    if (p0 / "inference.pdmodel").is_file() and (p0 / "inference.pdiparams").is_file():
        return p0
    return (Path.home() / ".paddleclas" / "inference_model" / "PULC" / "text_image_orientation").resolve()


def _get_pulc_legacy_predictor() -> _PulcLegacyPredictor:
    key = "pulc_legacy_text_image_orientation"
    cached = _PULC_CACHE.get(key)
    if cached is not None:
        return cached
    model_dir = _resolve_text_image_orientation_model_dir()
    pred = _PulcLegacyPredictor(str(model_dir))
    _PULC_CACHE[key] = pred
    return pred


def auto_rotate_upright_pulc_bgr(
    img_bgr: np.ndarray,
    *,
    max_long_edge: int = 1200,
) -> tuple[np.ndarray, int, dict[str, Any]]:
    """
    PULC 文字方向检测（0/90/180/270），旋转为正向。
    返回 (全分辨率 BGR, 顺时针 90° 步数 k, meta)。
    """
    meta: dict[str, Any] = {}
    if img_bgr.ndim != 3 or img_bgr.shape[2] != 3:
        meta["skipped"] = "not_bgr"
        return img_bgr, 0, meta

    try:
        legacy = _get_pulc_legacy_predictor()
    except ImportError:
        meta["skipped"] = "paddlepaddle_not_installed"
        return img_bgr, 0, meta
    except Exception as e:  # noqa: BLE001
        meta["skipped"] = "pulc_model_load_failed"
        meta["model_dir"] = str(_resolve_text_image_orientation_model_dir())
        meta["error"] = f"{type(e).__name__}: {e}"
        return img_bgr, 0, meta

    h, w = img_bgr.shape[:2]
    work = img_bgr
    if max_long_edge > 0:
        long = max(h, w)
        if long > max_long_edge:
            sc = max_long_edge / float(long)
            work = cv2.resize(img_bgr, (int(w * sc), int(h * sc)), interpolation=cv2.INTER_AREA)

    try:
        pred_k, lmeta = legacy.predict_k(work)
    except Exception as e:  # noqa: BLE001
        meta["skipped"] = "pulc_predict_failed"
        meta["error"] = f"{type(e).__name__}: {e}"
        return img_bgr, 0, meta

    meta["pulc"] = lmeta
    meta["model_dir"] = str(_resolve_text_image_orientation_model_dir())
    apply_k = (-int(pred_k)) % 4
    if apply_k == 2:
        meta["pulc_180_blocked"] = True
        meta["pulc_apply_k_raw"] = 2
        apply_k = 0
    meta["pulc_apply_k"] = apply_k
    return rotate_90_bgr(img_bgr, apply_k), apply_k, meta
