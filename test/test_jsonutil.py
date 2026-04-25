from __future__ import annotations

from mm_qbank.llm.jsonutil import parse_json_object


def test_parse_json_object_strips_fences() -> None:
    raw = '```json\n{"a": 1}\n```'
    assert parse_json_object(raw) == {"a": 1}
