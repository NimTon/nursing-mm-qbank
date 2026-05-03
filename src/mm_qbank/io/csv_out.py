from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Iterable, Sequence

from mm_qbank.io.xlsx_out import _as_修正状态_cell, refined_export_columns

_log = logging.getLogger(__name__)


def refined_csv_fieldnames(
    *,
    include_lecture_tips: bool = True,
    include_lecture_content: bool = True,
) -> tuple[str, ...]:
    """流式 CSV 列 = 精修表列 + 溯源列。"""
    return refined_export_columns(
        include_lecture_tips=include_lecture_tips,
        include_lecture_content=include_lecture_content,
    ) + ("page_id", "source_image")


def _needs_header(path: Path) -> bool:
    if not path.exists():
        return True
    try:
        return path.stat().st_size <= 0
    except OSError:
        return True


def append_refined_rows_to_csv(
    path: Path,
    rows: Iterable[dict[str, Any]],
    *,
    include_lecture_tips: bool = True,
    include_lecture_content: bool = True,
) -> int:
    """
    追加写入 refined 行到 CSV（UTF-8-SIG，便于 Excel 打开）。
    若文件不存在或为空则先写表头。
    """
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    columns = refined_csv_fieldnames(
        include_lecture_tips=include_lecture_tips,
        include_lecture_content=include_lecture_content,
    )

    wrote = 0
    write_header = _needs_header(path)
    if not write_header:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as rf:
                hdr = next(csv.reader(rf), None)
            if hdr is not None and list(hdr) != list(columns):
                _log.warning(
                    "已有流式 CSV 表头与当前导出列不一致（含讲师提醒/讲课内容列开关），追加后可能错位；建议删除后重跑：%s",
                    path,
                )
        except OSError:
            pass

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
