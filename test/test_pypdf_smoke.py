from __future__ import annotations

from pathlib import Path

import pytest


def test_pypdf_can_write_and_read_pdf(tmp_path: Path) -> None:
    try:
        from pypdf import PdfReader, PdfWriter
    except Exception as e:  # noqa: BLE001
        pytest.fail(
            "pypdf is not installed or cannot be imported.\n"
            "Install in project root:\n"
            "  pip install -e .\n"
            "or:\n"
            "  pip install pypdf\n"
            f"\nOriginal error: {e}\n"
        )

    out_pdf = tmp_path / "smoke.pdf"

    w = PdfWriter()
    w.add_blank_page(width=300, height=300)
    with out_pdf.open("wb") as f:
        w.write(f)

    assert out_pdf.is_file()
    r = PdfReader(str(out_pdf))
    assert len(r.pages) == 1

    # 空白页 extract_text 可能返回 None/""，但不应抛异常
    t = r.pages[0].extract_text()
    assert t is None or isinstance(t, str)

