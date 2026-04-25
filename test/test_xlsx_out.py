from __future__ import annotations

from pathlib import Path

from mm_qbank.io.xlsx_out import write_refined_rows_to_xlsx


def test_write_refined_xlsx_header(tmp_path: Path) -> None:
    p = tmp_path / "a.xlsx"
    write_refined_rows_to_xlsx(
        p,
        [
            {
                "page_id": "p1",
                "source_image": "x.png",
                "题号": "1",
                "原问题": "Q",
                "原解析": "A0",
                "修正后问题": "Q2",
                "修正问题原因": "r1",
                "修正问题参考来源": "b1",
                "修正后解析": "A1",
                "修正解析原因": "r2",
                "修正解析参考来源": "b2",
                "修正状态": True,
            }
        ],
    )
    assert p.is_file()
