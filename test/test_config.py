from __future__ import annotations

from pathlib import Path

import pytest

from mm_qbank.config import load_config, openai_settings, project_root


def test_project_root_points_to_repo_root() -> None:
    root = project_root()
    assert (root / "configs" / "default.yaml").is_file()


def test_load_default_config_has_vlm_refine() -> None:
    cfg = load_config(project_root() / "configs" / "default.yaml")
    assert "preprocess" in cfg
    assert "vlm" in cfg
    assert "refine" in cfg


def test_openai_settings_dashscope_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_TEXT_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    oa = openai_settings()
    assert oa["text_model"] == "qwen-plus"
