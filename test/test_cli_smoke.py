from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_verify_config_help_exits_zero() -> None:
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [sys.executable, "-m", "mm_qbank.cli", "verify-config", "-h"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0


def test_mm_qbank_cli_help_exits_zero() -> None:
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [sys.executable, "-m", "mm_qbank.cli", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0
    out = r.stdout or ""
    assert "lecture-scan" in out
    assert "correction-scan" in out
    assert "vlm-text" in out
    assert "vlm-refine" in out


def test_lecture_scan_subparser_help() -> None:
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [sys.executable, "-m", "mm_qbank.cli", "lecture-scan", "-h"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0
    assert "--in" in (r.stdout or "")


def test_correction_scan_subparser_help() -> None:
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run(
        [sys.executable, "-m", "mm_qbank.cli", "correction-scan", "-h"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0
    assert "--refine-model" in (r.stdout or "")
