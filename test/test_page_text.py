from __future__ import annotations

from pathlib import Path

from mm_qbank.layout_postprocess.page_text import (
    export_vlm_classified_artifact,
    vlm_classified_items_to_plaintext,
    write_page_text_manifest,
)


def test_vlm_classified_items_to_plaintext() -> None:
    s = vlm_classified_items_to_plaintext(
        [
            {"题号": "1", "类型": "问题", "内容": "题干"},
            {"题号": "1", "类型": "解析", "内容": "讲解"},
        ]
    )
    assert "问题" in s and "解析" in s and "题干" in s and "讲解" in s


def test_export_vlm_classified_artifact(tmp_path: Path) -> None:
    work = tmp_path / "w"
    j, t, row = export_vlm_classified_artifact(
        work=work,
        page_id="p1",
        source_image=tmp_path / "a.jpg",
        items=[{"题号": "1", "类型": "问题", "内容": "X"}],
        subdir="pages",
    )
    data = (work / "pages" / "p1.json").read_text(encoding="utf-8")
    assert "问题" in data
    assert row.get("structured_file", "").endswith("p1.json")
    assert row.get("text_file", "").endswith("p1.txt")
    assert j == work / "pages" / "p1.json"
    assert t == work / "pages" / "p1.txt"


def test_write_page_text_manifest_vlm_row(tmp_path: Path) -> None:
    work = tmp_path / "w"
    _, __, row = export_vlm_classified_artifact(
        work=work,
        page_id="p1",
        source_image=tmp_path / "a.jpg",
        items=[{"题号": "1", "类型": "问题", "内容": "X"}],
        subdir="pages",
    )
    p = write_page_text_manifest(work, "pages", "pages.jsonl", [row])
    assert p is not None
    assert "p1.json" in p.read_text(encoding="utf-8")
