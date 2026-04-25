from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_verify_config_help_exits_zero() -> None:
    root = Path(__file__).resolve().parents[1]
    exe = sys.executable
    r = subprocess.run(
        [exe, "-m", "mm_qbank.cli", "verify-config", "-h"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0
    out = r.stdout or ""
    assert "VLM" in out or "vlm" in out.lower()


def test_mm_qbank_cli_help_exits_zero() -> None:
    root = Path(__file__).resolve().parents[1]
    exe = sys.executable
    r = subprocess.run(
        [exe, "-m", "mm_qbank.cli", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0
    out = r.stdout or ""
    assert "vlm-text" in out
    assert "llm-compose" in out
    assert "vlm-refine" in out


def test_vlm_refine_subparser_help() -> None:
    root = Path(__file__).resolve().parents[1]
    exe = sys.executable
    r = subprocess.run(
        [exe, "-m", "mm_qbank.cli", "vlm-refine", "-h"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0
    out = r.stdout or ""
    assert "--manifest" in out


def test_vlm_text_subparser_help() -> None:
    root = Path(__file__).resolve().parents[1]
    exe = sys.executable
    r = subprocess.run(
        [exe, "-m", "mm_qbank.cli", "vlm-text", "-h"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0
    out = r.stdout or ""
    assert "--in" in out
    assert "--model" in out
    assert "VLM_MODEL" in out


def test_vlm_text_accepts_verbose_after_subcommand_not_unrecognized(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    exe = sys.executable
    empty = tmp_path / "no_images"
    empty.mkdir()
    r = subprocess.run(
        [exe, "-m", "mm_qbank.cli", "vlm-text", "--in", str(empty), "-v"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    out = (r.stderr or "") + (r.stdout or "")
    assert "unrecognized arguments" not in out


def test_vlm_text_accepts_verbose_before_subcommand(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    exe = sys.executable
    empty = tmp_path / "no_images"
    empty.mkdir()
    r = subprocess.run(
        [exe, "-m", "mm_qbank.cli", "-v", "vlm-text", "--in", str(empty)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    out = (r.stderr or "") + (r.stdout or "")
    assert "unrecognized arguments" not in out
