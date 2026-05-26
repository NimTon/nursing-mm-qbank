from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


def project_root() -> Path:
    # 开发态：src/mm_qbank/config.py → 项目根为 parents[2]
    # 打包态（PyInstaller）：__file__ 位于临时解压目录；以 exe 所在目录作为“项目根”（旁置 configs/ 与 .env）
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def vlm_settings() -> dict[str, str | None]:
    """
    多模态 VLM 网关：``VLM_BASE_URL``、``VLM_API_KEY``、``VLM_MODEL``（未设模型时默认 ``gpt-4o``）。
    """
    b = (os.getenv("VLM_BASE_URL") or "").strip()
    k = (os.getenv("VLM_API_KEY") or "").strip()
    mm = (os.getenv("VLM_MODEL") or "").strip() or "gpt-4o"
    return {
        "api_key": k or None,
        "base_url": b or None,
        "mm_model": mm,
    }


def llm_text_settings() -> dict[str, str | None]:
    """
    纯文本 LLM：``LLM_BASE_URL``、``LLM_API_KEY``、``LLM_MODEL``（未设模型时默认 ``gpt-4o-mini``）。

    用于 llm-compose、vlm-refine、xlsx-lecture-tips、xlsx-lecture-content。
    """
    b = (os.getenv("LLM_BASE_URL") or "").strip()
    k = (os.getenv("LLM_API_KEY") or "").strip()
    m = (os.getenv("LLM_MODEL") or "").strip() or "gpt-4o-mini"
    return {
        "api_key": k or None,
        "base_url": b or None,
        "text_model": m,
    }


def _yaml_model(cfg: dict[str, Any], *paths: tuple[str, ...]) -> str:
    for path in paths:
        node: Any = cfg
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if node is not None and str(node).strip() and str(node).strip().lower() != "null":
            return str(node).strip()
    return ""


def resolve_vlm_model(cfg: dict[str, Any], *, override: str | None = None) -> str:
    """模型优先级：CLI 覆盖 > .env ``VLM_MODEL`` > yaml（兼容旧配置）> 默认 ``gpt-4o``。"""
    if (override or "").strip():
        return override.strip()
    explicit = (os.getenv("VLM_MODEL") or "").strip()
    if explicit:
        return explicit
    yaml_m = _yaml_model(cfg, ("lecture_scan", "vlm", "model"), ("vlm", "model"))
    if yaml_m:
        return yaml_m
    return "gpt-4o"


def resolve_llm_model(cfg: dict[str, Any], *, override: str | None = None) -> str:
    """模型优先级：CLI 覆盖 > .env ``LLM_MODEL`` > yaml（兼容旧配置）> 默认 ``gpt-4o-mini``。"""
    if (override or "").strip():
        return override.strip()
    explicit = (os.getenv("LLM_MODEL") or "").strip()
    if explicit:
        return explicit
    yaml_m = _yaml_model(
        cfg,
        ("lecture_content", "model"),
        ("refine", "model"),
        ("lecture_scan", "assemble", "model"),
    )
    if yaml_m:
        return yaml_m
    return llm_text_settings()["text_model"] or "gpt-4o-mini"


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    root = project_root()
    load_dotenv(root / ".env")
    if config_path is not None:
        path = config_path
    else:
        # 优先使用 exe 同目录的 configs（便于用户覆盖）；若打包在 _internal/_MEIPASS 下，则回退读取内置 configs。
        path = root / "configs" / "default.yaml"
        if (not path.exists()) and getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
            path = Path(sys._MEIPASS) / "configs" / "default.yaml"  # type: ignore[attr-defined]
    if path.exists() and path.is_dir():
        raise ValueError(f"--config 需要指向 YAML 文件，但你给的是目录: {path}")
    if not path.exists():
        raise FileNotFoundError(f"未找到配置文件: {path}")
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data
