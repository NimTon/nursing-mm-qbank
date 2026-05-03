from __future__ import annotations

import json

import pytest

from mm_qbank.llm.jsonutil import parse_json_object


def test_parse_json_object_strips_fences() -> None:
    raw = '```json\n{"a": 1}\n```'
    assert parse_json_object(raw) == {"a": 1}


def test_parse_json_object_repairs_raw_newline_inside_string() -> None:
    """模型常在 ``讲课内容`` 等多行文本里直接换行，导致标准 JSON 解析失败。"""
    broken = '{"items":[{"题号":"1","要点":"第一行\n第二行"}]}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken)
    assert parse_json_object(broken) == {"items": [{"题号": "1", "要点": "第一行\n第二行"}]}


def test_parse_json_object_balanced_extract_with_prefix_junk() -> None:
    raw = '说明如下：\n{"items": [{"x": 1}]}\n谢谢'
    assert parse_json_object(raw) == {"items": [{"x": 1}]}
