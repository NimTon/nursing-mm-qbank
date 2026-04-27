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


def refine_system(*, include_lecture_tips: bool = False) -> str:
    base = get_prompt("refine_system.txt", _fb.REFINE_SYSTEM)
    if include_lecture_tips:
        addon = get_prompt("refine_system_lecture_addon.txt", _fb.REFINE_SYSTEM_LECTURE_ADDON)
        base = f"{base.rstrip()}\n\n{addon.strip()}\n"
    return base


def refine_user_payload(
    *,
    page_id: str,
    merged: list[dict[str, str]],
    web_search: bool,
    include_lecture_tips: bool = False,
) -> str:
    if include_lecture_tips:
        ex = get_prompt("refine_example_with_lecture.txt", _fb.REFINE_EXAMPLE_WITH_LECTURE)
    else:
        ex = get_prompt("refine_example.txt", _fb.REFINE_EXAMPLE)
    w = "已开启" if web_search else "已关闭"
    body = get_prompt("refine_user.txt", _fb.REFINE_USER)
    block = ""
    if include_lecture_tips:
        b2 = get_prompt("refine_user_lecture_block.txt", _fb.REFINE_USER_LECTURE_BLOCK)
        block = b2.strip() + "\n\n"
    if "__REFINE_LECTURE_BLOCK__" in body:
        body = body.replace("__REFINE_LECTURE_BLOCK__", block)
    elif include_lecture_tips and block.strip():
        anchor = "只输出最外层一个 JSON 对象"
        if anchor in body:
            body = body.replace(anchor, block + anchor, 1)
        else:
            body = block + body
            _log.warning(
                "refine_user 模板缺少 __REFINE_LECTURE_BLOCK__ 且未找到插入锚点，已将同批「讲师提醒」说明置于提示词开头"
            )
    return (
        body.replace("__REFINE_EXAMPLE__", ex.strip())
        .replace("__WEB_STATUS__", w)
        .replace("__PAGE_ID__", page_id)
        .replace("__MERGED_JSON__", json.dumps(merged, ensure_ascii=False, indent=2))
    )


def lecture_tips_system() -> str:
    return get_prompt("lecture_tips_system.txt", _fb.LECTURE_TIPS_SYSTEM)


def lecture_tips_user_payload(
    *,
    batch_id: str,
    items: list[dict[str, str | int]],
    web_search: bool,
) -> str:
    ex = get_prompt("lecture_tips_example.txt", _fb.LECTURE_TIPS_EXAMPLE)
    w = "已开启" if web_search else "已关闭"
    body = get_prompt("lecture_tips_user.txt", _fb.LECTURE_TIPS_USER)
    return (
        body.replace("__LECTURE_TIPS_EXAMPLE__", ex.strip())
        .replace("__WEB_STATUS__", w)
        .replace("__BATCH_ID__", batch_id)
        .replace("__ITEMS_JSON__", json.dumps(items, ensure_ascii=False, indent=2))
    )
