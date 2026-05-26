from __future__ import annotations

from pathlib import Path

import pytest

from mm_qbank.config import (
    load_config,
    llm_text_settings,
    project_root,
    resolve_llm_model,
    resolve_vlm_model,
    vlm_settings,
)


def test_package_version() -> None:
    from mm_qbank import __version__

    assert __version__ == "0.1.9"


def test_project_root_points_to_repo_root() -> None:
    root = project_root()
    assert (root / "configs" / "default.yaml").is_file()
    assert (root / "configs" / "版本摘要.txt").is_file()


def test_load_default_config_has_scan_refine() -> None:
    cfg = load_config(project_root() / "configs" / "default.yaml")
    assert "preprocess" in cfg
    assert "lecture_scan" in cfg
    assert "refine" in cfg
    assert "lecture_content" in cfg
    assert "web_search" in (cfg.get("refine") or {})
    assert "stream_refine" not in (cfg.get("refine") or {})


def test_vlm_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("VLM_BASE_URL", "VLM_API_KEY", "VLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VLM_BASE_URL", "https://vlm.example/v1")
    monkeypatch.setenv("VLM_API_KEY", "k-vlm")
    monkeypatch.setenv("VLM_MODEL", "mm-1")
    v = vlm_settings()
    assert v["base_url"] == "https://vlm.example/v1"
    assert v["api_key"] == "k-vlm"
    assert v["mm_model"] == "mm-1"


def test_vlm_settings_default_mm_model(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("VLM_BASE_URL", "VLM_API_KEY", "VLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("VLM_API_KEY", "x")
    v = vlm_settings()
    assert v["mm_model"] == "gpt-4o"


def test_llm_text_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "k-llm")
    monkeypatch.setenv("LLM_MODEL", "text-1")
    t = llm_text_settings()
    assert t["base_url"] == "https://llm.example/v1"
    assert t["api_key"] == "k-llm"
    assert t["text_model"] == "text-1"


def test_llm_text_settings_default_text_model(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_API_KEY", "x")
    t = llm_text_settings()
    assert t["text_model"] == "gpt-4o-mini"


def test_resolve_vlm_model_prefers_env_over_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLM_MODEL", "from-env")
    cfg = {"lecture_scan": {"vlm": {"model": "from-yaml"}}}
    assert resolve_vlm_model(cfg) == "from-env"


def test_resolve_llm_model_yaml_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    cfg = {"refine": {"model": "qwen3-max"}}
    assert resolve_llm_model(cfg) == "qwen3-max"
