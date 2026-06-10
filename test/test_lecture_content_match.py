from __future__ import annotations

from mm_qbank.pipeline.lecture_content_run import _build_lc_by_bucket_from_parsed


def test_lc_match_by_chunk_order() -> None:
    chunk = [{"题号": "16", "题目类型": "单选"}]
    parsed = [{"题号": "16", "要点": "要点A", "讲课内容": "讲课B"}]
    by_out = _build_lc_by_bucket_from_parsed(parsed, chunk)
    pt, lc = by_out["16\x1f单选"]
    assert pt == "要点A"
    assert lc == "讲课B"
