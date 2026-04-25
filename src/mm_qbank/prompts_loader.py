from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from mm_qbank.config import project_root
from mm_qbank import prompt_builtins as _fb

_log = logging.getLogger(__name__)


def _candidates(name: str) -> list[Path]:
    root = project_root()
    out: list[Path] = [root / "configs" / "prompts" / name]
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        out.append(Path(sys._MEIPASS) / "configs" / "prompts" / name)  # type: ignore[attr-defined]
    return out


def load_prompt_file(name: str) -> str | None:
    """返回 UTF-8 文本；未找到任一路径时返回 None。"""
    for p in _candidates(name):
        if p.is_file():
            return p.read_text(encoding="utf-8")
    return None


def get_prompt(name: str, default: str) -> str:
    t = load_prompt_file(name)
    if t is not None:
        return t
    _log.info(
        "未找到外置提示词 %s，使用内置默认。打包后可在「程序同目录/configs/prompts/」放置该文件以覆盖。",
        name,
    )
    return default


def refine_system() -> str:
    return get_prompt("refine_system.txt", _fb.REFINE_SYSTEM)


def refine_user_payload(
    *,
    page_id: str,
    merged: list[dict[str, str]],
    web_search: bool,
) -> str:
    ex = get_prompt("refine_example.txt", _fb.REFINE_EXAMPLE)
    w = "已开启" if web_search else "已关闭"
    body = get_prompt("refine_user.txt", _fb.REFINE_USER)
    return (
        body.replace("__REFINE_EXAMPLE__", ex.strip())
        .replace("__WEB_STATUS__", w)
        .replace("__PAGE_ID__", page_id)
        .replace("__MERGED_JSON__", json.dumps(merged, ensure_ascii=False, indent=2))
    )
