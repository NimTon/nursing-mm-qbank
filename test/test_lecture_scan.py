from __future__ import annotations

from mm_qbank.pipeline.lecture_scan_run import (
    build_concatenated_raw_text,
    filter_noise_lines,
    lecture_scan_handout_docx_path,
    parse_page_number_for_sort,
    resolve_export_chapter_title,
    sanitize_chapter_title_for_filename,
    sort_scan_pages,
)


def test_parse_page_number_for_sort() -> None:
    assert parse_page_number_for_sort("— 153 —", file_order=5) == (153, 5)
    assert parse_page_number_for_sort("", file_order=3)[0] == 10**9


def test_filter_noise_lines() -> None:
    lines = [
        "1. 正常题干",
        "启峻教育让梦想起飞的地方!",
        "启峻教育祝您考试成功",
        "报名热线:17501446985",
        "A. 选项",
    ]
    out = filter_noise_lines(lines)
    assert out == ["1. 正常题干", "A. 选项"]


def test_sort_scan_pages_by_page_number() -> None:
    rows = [
        {"页码": "155", "file_order": 2, "scan_file": "a.json"},
        {"页码": "153", "file_order": 1, "scan_file": "b.json"},
        {"页码": "", "file_order": 3, "scan_file": "c.json"},
    ]
    sorted_rows = sort_scan_pages(rows)
    assert [r["页码"] for r in sorted_rows] == ["153", "155", ""]


def test_build_concatenated_raw_text(tmp_path) -> None:
    scan_dir = tmp_path / "scan_pages"
    scan_dir.mkdir()
    doc = {"lines": ["第一行", "第二行"]}
    (scan_dir / "p1.json").write_text(
        __import__("json").dumps(doc, ensure_ascii=False),
        encoding="utf-8",
    )
    pages = [{"页码": "10", "scan_file": "scan_pages/p1.json", "file_order": 1}]
    text = build_concatenated_raw_text(pages, work=tmp_path)
    assert "=== 页码 10 ===" in text
    assert "第一行" in text and "第二行" in text


def test_resolve_export_chapter_title() -> None:
    pages = [
        {"页码": "2", "file_order": 2, "章节标题": ""},
        {"页码": "1", "file_order": 1, "章节标题": "第一章 绪论"},
    ]
    assert resolve_export_chapter_title(pages) == "第一章 绪论"
    assert resolve_export_chapter_title([{"页码": "1", "章节标题": ""}]) == ""


def test_lecture_scan_handout_docx_path(tmp_path) -> None:
    p = lecture_scan_handout_docx_path(tmp_path, "第一章 绪论")
    assert p.name == "第一章_绪论_讲课稿.docx"
    p2 = lecture_scan_handout_docx_path(tmp_path, "")
    assert p2.name == "lecture_scan_讲课稿.docx"


def test_sanitize_chapter_title_for_filename() -> None:
    assert sanitize_chapter_title_for_filename("第一章 绪论") == "第一章_绪论"
    assert sanitize_chapter_title_for_filename("") == ""
