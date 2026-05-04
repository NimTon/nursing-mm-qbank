from __future__ import annotations

from mm_qbank.pipeline.vlm_merge import (
    aggregate_bucket_key,
    merge_sort_key_for_row,
    merge_vlm_items_by_tihao,
)


def test_merge_vlm_by_tihao() -> None:
    items = [
        {"题号": "1", "类型": "问题", "题目类型": "单选", "内容": "Q1 line"},
        {"题号": "1", "类型": "解析", "内容": "A1 line"},
        {"题号": "2", "类型": "问题", "内容": "Q2"},
    ]
    m = merge_vlm_items_by_tihao(items)
    assert len(m) == 2
    d1 = next(x for x in m if x["题号"] == "1")
    assert d1["题目类型"] == "单选"
    assert "Q1" in d1["问题"] and "A1" in d1["解析"]
    d2 = next(x for x in m if x["题号"] == "2")
    assert d2["题目类型"] == "未知"
    assert d2["问题"] == "Q2" and d2["解析"] == ""


def test_merge_same_tihao_different_question_kind_stays_separate() -> None:
    """单选第 10 题与多选第 10 题题号相同但不得合并为一题。"""
    items = [
        {"题号": "10", "类型": "问题", "题目类型": "单选", "内容": "单选题干"},
        {"题号": "10", "类型": "解析", "题目类型": "单选", "内容": "单选解析"},
        {"题号": "10", "类型": "问题", "题目类型": "多选", "内容": "多选题干"},
        {"题号": "10", "类型": "解析", "题目类型": "多选", "内容": "多选解析"},
    ]
    m = merge_vlm_items_by_tihao(items)
    assert len(m) == 2
    by_kind = {x["题目类型"]: x for x in m}
    assert "单选" in by_kind and "多选" in by_kind
    assert "单选题干" in by_kind["单选"]["问题"] and "单选解析" in by_kind["单选"]["解析"]
    assert "多选题干" in by_kind["多选"]["问题"] and "多选解析" in by_kind["多选"]["解析"]
    assert aggregate_bucket_key("10", "单选") != aggregate_bucket_key("10", "多选")


def test_merge_sort_order_kind_before_tihao() -> None:
    """判断(30) 题号 2 应排在 单选(10) 题号 10 之后（题型优先再题号）。"""
    items = [
        {"题号": "10", "类型": "问题", "题目类型": "单选", "内容": "q10"},
        {"题号": "2", "类型": "问题", "题目类型": "判断", "内容": "q2"},
    ]
    m = merge_vlm_items_by_tihao(items)
    assert [x["题号"] for x in m] == ["10", "2"]
    rows = [{"题号": x["题号"], "题目类型": x["题目类型"]} for x in m]
    sorted_rows = sorted(rows, key=merge_sort_key_for_row)
    assert [r["题号"] for r in sorted_rows] == ["10", "2"]


def test_merge_empty_tihao() -> None:
    m = merge_vlm_items_by_tihao(
        [
            {"题号": "", "类型": "问题", "内容": " only "},
        ]
    )
    assert len(m) == 1
    assert m[0]["题号"] == ""
