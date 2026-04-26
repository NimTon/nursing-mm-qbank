from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable, Sequence

from mm_qbank.io.xlsx_out import _as_修正状态_cell


_BASE_REFINED_CSV_COLUMNS: Sequence[str] = (
    "题号",
    "修正状态",
    "原问题",
    "原解析",
    "修正后问题",
    "修正问题原因",
    "修正问题参考来源",
    "修正后解析",
    "修正解析原因",
    "修正解析参考来源",
    "page_id",
    "source_image",
)

_OFFLINE_KB_COLUMNS: Sequence[str] = ("离线参考1", "离线参考2", "离线参考3")


def _needs_header(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        return path.stat().st_size <= 0
    except OSError:
        return True


def append_refined_rows_to_csv(
    path: Path, rows: Iterable[dict[str, Any]], *, include_offline_refs: bool = True
) -> int:
    """
    追加写入 refined 行到 CSV（UTF-8-SIG，便于 Excel 打开）。
    若文件不存在或为空则先写表头。
    """
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    wrote = 0
    write_header = _needs_header(path)
    columns = (
        tuple(_BASE_REFINED_CSV_COLUMNS) + tuple(_OFFLINE_KB_COLUMNS) if include_offline_refs else tuple(_BASE_REFINED_CSV_COLUMNS)
    )
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(columns), extrasaction="ignore")
        if write_header:
            w.writeheader()
        for row in rows:
            out = dict(row)
            out["修正状态"] = _as_修正状态_cell(out.get("修正状态"))
            w.writerow({k: ("" if out.get(k) is None else out.get(k)) for k in columns})
            wrote += 1
        f.flush()
    return wrote

