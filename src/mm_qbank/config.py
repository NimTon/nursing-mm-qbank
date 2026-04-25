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


def _is_dashscope_compatible_base_url(base_url: str | None) -> bool:
    u = (base_url or "").lower()
    return "dashscope" in u


def _is_deepseek_base_url(base_url: str | None) -> bool:
    u = (base_url or "").lower()
    return "deepseek.com" in u


def _default_models_for_base_url(base_url: str | None) -> tuple[str, str]:
    if _is_dashscope_compatible_base_url(base_url):
        return ("qwen-plus", "qwen-vl-plus")
    # OpenAI 兼容，见 https://api-docs.deepseek.com/zh-cn/
    if _is_deepseek_base_url(base_url):
        return ("deepseek-v4-pro", "deepseek-v4-pro")
    return ("gpt-4o-mini", "gpt-4o")


def openai_settings() -> dict[str, str | None]:
    base_url = os.getenv("OPENAI_BASE_URL")
    key_openai = os.getenv("OPENAI_API_KEY")
    key_dash = os.getenv("DASHSCOPE_API_KEY")
    key_deepseek = os.getenv("DEEPSEEK_API_KEY")
    if _is_deepseek_base_url(base_url):
        # 见 https://api-docs.deepseek.com/zh-cn/ — 可单独用 DEEPSEEK_API_KEY，与百炼的 OPENAI_API_KEY 同时存在时以本项优先
        api_key = key_deepseek or key_openai or key_dash
    else:
        api_key = key_openai or key_dash
    dt, dmm = _default_models_for_base_url(base_url)
    text_model = os.getenv("OPENAI_TEXT_MODEL") or dt
    final_model = os.getenv("OPENAI_FINAL_PROOFREAD_MODEL") or text_model
    mm_model = os.getenv("OPENAI_MM_MODEL") or dmm
    return {
        "api_key": api_key,
        "base_url": base_url,
        "text_model": text_model,
        "final_model": final_model,
        "mm_model": mm_model,
    }


def text_llm_settings() -> dict[str, str | None]:
    """
    纯文本 LLM（llm-compose、vlm-refine）的网关，可与多模态主网关分离。

    1) 若同时设置 ``TEXT_LLM_BASE_URL`` 与 ``TEXT_LLM_API_KEY`` → 文本专用（可指向 DeepSeek 等）。
    2) 否则若主 ``OPENAI_BASE_URL`` 非 DeepSeek 且设置了 ``DEEPSEEK_API_KEY`` → 文本默认走
       ``TEXT_LLM_BASE_URL`` / ``DEEPSEEK_BASE_URL`` / ``https://api.deepseek.com`` + 该 key
       （典型：VLM 走百炼 Qwen，拆题/修正走 DeepSeek）。
    3) 否则与 ``openai_settings()`` 相同（同一 key/base）。
    文本模型名优先 ``TEXT_LLM_MODEL``；在 2) 且未设时默认 ``deepseek-v4-flash``（更省）。
    """
    oa = openai_settings()
    k_txt = os.getenv("TEXT_LLM_API_KEY")
    b_txt = os.getenv("TEXT_LLM_BASE_URL")
    if k_txt and b_txt:
        m_env = os.getenv("TEXT_LLM_MODEL")
        if m_env:
            m = m_env.strip()
        elif _is_deepseek_base_url(b_txt):
            m = "deepseek-v4-flash"
        else:
            m = str(oa.get("text_model") or "gpt-4o-mini")
        return {"api_key": k_txt.strip(), "base_url": b_txt.strip(), "text_model": m}

    k_ds = os.getenv("DEEPSEEK_API_KEY")
    if k_ds and not _is_deepseek_base_url(oa.get("base_url")):
        base = (
            os.getenv("TEXT_LLM_BASE_URL")
            or os.getenv("DEEPSEEK_BASE_URL")
            or "https://api.deepseek.com"
        ).strip()
        m = (os.getenv("TEXT_LLM_MODEL") or "deepseek-v4-flash").strip()
        return {"api_key": k_ds.strip(), "base_url": base, "text_model": m}

    return {
        "api_key": oa.get("api_key"),
        "base_url": oa.get("base_url"),
        "text_model": str(oa.get("text_model") or "gpt-4o-mini"),
    }


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
