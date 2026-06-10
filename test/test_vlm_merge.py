from __future__ import annotations

from mm_qbank.pipeline.vlm_merge import (
    aggregate_bucket_key,
    merge_vlm_items_by_tihao,
    row_aggregate_key,
)


def test_merge_same_tihao_single_and_multi_stay_separate() -> None:
    items = [
        {"题号": "1", "类型": "问题", "题目类型": "单选", "内容": "单选题干"},
        {"题号": "1", "类型": "解析", "题目类型": "单选", "内容": "单选解析"},
        {"题号": "1", "类型": "问题", "题目类型": "多选", "内容": "多选题干"},
        {"题号": "1", "类型": "解析", "题目类型": "多选", "内容": "多选解析"},
    ]
    out = merge_vlm_items_by_tihao(items)
    assert len(out) == 2
    by_kind = {x["题目类型"]: x for x in out}
    assert "单选题干" in by_kind["单选"]["问题"]
    assert "单选解析" in by_kind["单选"]["解析"]
    assert "多选题干" in by_kind["多选"]["问题"]
    assert "多选解析" in by_kind["多选"]["解析"]


def test_row_aggregate_key_uses_kind() -> None:
    a = row_aggregate_key({"题号": "3", "题目类型": "单选"})
    b = row_aggregate_key({"题号": "3", "题目类型": "多选"})
    assert a != b
    assert a == aggregate_bucket_key("3", "单选")
