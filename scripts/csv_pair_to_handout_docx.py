"""
将「修正流式 CSV」与「讲课内容流式 CSV」合并后导出讲义 Word（与 GUI 流水线 docx 结构一致）。

示例（050301 批次）::

    python scripts/csv_pair_to_handout_docx.py ^
      --refine-csv data/out/vlm_gui_20260503_172138_050301/refined_merged_stream_050301.csv ^
      --lecture-csv data/out/vlm_gui_20260503_172138_050301/refined_merged_050301_lecture_content_stream.csv ^
      --out-docx data/out/vlm_gui_20260503_172138_050301/handout.docx
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mm_qbank.io.docx_out import write_lecture_handout_docx  # noqa: E402


def _read_csv(path: Path) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8-sig")
    r = csv.DictReader(io.StringIO(text))
    out: list[dict[str, str]] = []
    for row in r:
        out.append({k: (v if v is not None else "") for k, v in row.items()})
    return out


def _lecture_by_tihao(rows: list[dict[str, str]]) -> dict[str, tuple[str, str]]:
    m: dict[str, tuple[str, str]] = {}
    for row in rows:
        th = str(row.get("题号") or "").strip()
        if not th:
            continue
        m[th] = (
            str(row.get("要点") or "").strip(),
            str(row.get("讲课内容") or "").strip(),
        )
    return m


def merge_rows_for_handout(
    refine_rows: list[dict[str, str]],
    lecture_by_tihao: dict[str, tuple[str, str]],
    *,
    lecture_overrides: bool = True,
) -> list[dict[str, str]]:
    """按修正 CSV 的行顺序输出；``lecture_overrides`` 为真时用讲课 CSV 覆盖同名题的要点/讲课内容。"""
    out: list[dict[str, str]] = []
    for raw in refine_rows:
        th = str(raw.get("题号") or "").strip()
        row = dict(raw)
        if th and th in lecture_by_tihao:
            pt, lc = lecture_by_tihao[th]
            if lecture_overrides or not str(row.get("要点") or "").strip():
                row["要点"] = pt
            if lecture_overrides or not str(row.get("讲课内容") or "").strip():
                row["讲课内容"] = lc
        out.append(row)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="两个 CSV 合并导出讲义 Word")
    ap.add_argument(
        "--refine-csv",
        type=Path,
        required=True,
        help="修正流式表（含题号、原/修正问题与解析等），如 refined_merged_stream_*.csv",
    )
    ap.add_argument(
        "--lecture-csv",
        type=Path,
        required=True,
        help="讲课内容流式表（题号、要点、讲课内容），如 *_lecture_content_stream.csv",
    )
    ap.add_argument(
        "--out-docx",
        type=Path,
        required=True,
        help="输出 .docx 路径",
    )
    ap.add_argument(
        "--no-lecture-override",
        action="store_true",
        help="仅当修正表里要点/讲课内容为空时才填入讲课 CSV（默认：讲课 CSV 优先覆盖）",
    )
    args = ap.parse_args()

    refine_path = args.refine_csv.resolve()
    lecture_path = args.lecture_csv.resolve()
    out_docx = args.out_docx.resolve()

    if not refine_path.is_file():
        raise SystemExit(f"未找到修正 CSV: {refine_path}")
    if not lecture_path.is_file():
        raise SystemExit(f"未找到讲课 CSV: {lecture_path}")

    refine_rows = _read_csv(refine_path)
    lecture_rows = _read_csv(lecture_path)
    lc_map = _lecture_by_tihao(lecture_rows)

    merged = merge_rows_for_handout(
        refine_rows,
        lc_map,
        lecture_overrides=not args.no_lecture_override,
    )
    if not merged:
        raise SystemExit("修正 CSV 无数据行，放弃导出")

    write_lecture_handout_docx(out_docx, merged)
    print(f"已写入: {out_docx}")


if __name__ == "__main__":
    main()
