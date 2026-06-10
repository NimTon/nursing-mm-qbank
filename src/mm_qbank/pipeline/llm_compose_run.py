from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from mm_qbank.pipeline.vlm_text_run import _wait_unpaused

_log = logging.getLogger(__name__)


def _manifest_out_root(manifest_path: Path) -> Path:
    """manifest 位于 {out}/pages/pages.jsonl → 返回 {out}。"""
    return manifest_path.resolve().parent.parent


def _read_manifest_lines(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def run_llm_compose_manifest(
    *,
    manifest_path: Path,
    out_jsonl: Path,
    config_path: Path | None = None,
    model: str | None = None,
    cancel_event: threading.Event | None = None,
    paused: Callable[[], bool] | None = None,
    on_page_done: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """
    **离线提取**：从 ``vlm-text`` 的 ``structured_file`` 中读取 VLM 同次返回的 ``composed_items``，
    写入 ``out_jsonl``（每行一页的 JSON）。

    说明：为了将「VLM 整页读图」与「拆题」合并为一次模型调用，本项目不再在此阶段调用在线 LLM。
    """
    mpath = manifest_path.resolve()
    if not mpath.is_file():
        raise FileNotFoundError(f"未找到 manifest: {mpath}")

    root = _manifest_out_root(mpath)
    _log.info("llm-compose manifest: %s", mpath)
    _log.info("structured 根目录: %s", root)
    rows = _read_manifest_lines(mpath)
    _log.info("manifest 共 %s 页，将从 VLM 结果提取 composed_items", len(rows))
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)

    n_ok = 0
    cancelled = False
    n_rows = len(rows)
    if n_rows < 1:
        out_jsonl.write_text("", encoding="utf-8")
        return {
            "manifest": str(mpath),
            "out_jsonl": str(out_jsonl.resolve()),
            "n_pages": 0,
            "n_written": 0,
            "mode": "offline_extract",
            "cancelled": False,
        }

    with out_jsonl.open("w", encoding="utf-8") as fout:
        for idx, row in enumerate(rows, start=1):
            if _wait_unpaused(pause=paused, cancel=cancel_event):
                cancelled = True
                _log.info("llm-compose 在页 %s 前被用户取消/结束", idx)
                break
            page_id = str(row.get("page_id", ""))
            rel = str(row.get("structured_file", "")).strip().replace("\\", "/")
            spath = (root / rel).resolve()
            if not spath.is_file():
                _log.warning("[%s/%s] page_id=%s 跳过：找不到 structured_file %s", idx, n_rows, page_id, spath)
                out_row = {"page_id": page_id, "error": f"找不到 structured_file: {spath}", "items": []}
            else:
                data = cast(dict[str, Any], json.loads(spath.read_text(encoding="utf-8")))
                items = data.get("composed_items")
                if not isinstance(items, list):
                    items = []
                    out_row = {
                        "page_id": page_id,
                        "error": "structured_file 中未包含 composed_items（请更新 VLM 提示词 vlm_user.txt）",
                        "items": [],
                    }
                else:
                    out_row = {"page_id": page_id, "source_image": row.get("source_image"), "items": items}
                    n_ok += 1
            fout.write(json.dumps(out_row, ensure_ascii=False) + "\n")
            if on_page_done is not None and n_rows > 0:
                on_page_done(idx, n_rows)

    _log.info("完成：成功提取 composed_items 的页=%s/%s -> %s", n_ok, len(rows), out_jsonl.resolve())
    return {
        "manifest": str(mpath),
        "out_jsonl": str(out_jsonl.resolve()),
        "n_pages": len(rows),
        "n_written": n_ok,
        "mode": "offline_extract",
        "cancelled": cancelled,
    }
