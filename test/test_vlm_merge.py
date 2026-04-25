from __future__ import annotations

from mm_qbank.pipeline.vlm_merge import merge_vlm_items_by_tihao


def test_merge_vlm_by_tihao() -> None:
    items = [
        {"题号": "1", "类型": "问题", "内容": "Q1 line"},
        {"题号": "1", "类型": "解析", "内容": "A1 line"},
        {"题号": "2", "类型": "问题", "内容": "Q2"},
    ]
    m = merge_vlm_items_by_tihao(items)
    assert len(m) == 2
    d1 = next(x for x in m if x["题号"] == "1")
    assert "Q1" in d1["问题"] and "A1" in d1["解析"]
    d2 = next(x for x in m if x["题号"] == "2")
    assert d2["问题"] == "Q2" and d2["解析"] == ""


def test_merge_empty_tihao() -> None:
    m = merge_vlm_items_by_tihao(
        [
            {"题号": "", "类型": "问题", "内容": " only "},
        ]
    )
    assert len(m) == 1
    assert m[0]["题号"] == ""
