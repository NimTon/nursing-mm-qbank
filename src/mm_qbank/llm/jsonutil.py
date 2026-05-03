from __future__ import annotations

import json
import re
from typing import Any


def _sanitize_raw_chars_inside_json_strings(s: str) -> str:
    """
    LLM 常在 JSON 字符串值内直接换行或未转义控制字符，导致 ``json.loads`` 报
    ``Unterminated string``。仅在双引号字符串内部把裸换行等转为 JSON 转义形式。
    """
    out: list[str] = []
    i = 0
    n = len(s)
    in_string = False
    escape = False
    while i < n:
        c = s[i]
        if escape:
            out.append(c)
            escape = False
            i += 1
            continue
        if in_string:
            if c == "\\":
                out.append(c)
                escape = True
                i += 1
                continue
            if c == '"':
                in_string = False
                out.append(c)
                i += 1
                continue
            o = ord(c)
            if c == "\n":
                out.append("\\n")
                i += 1
                continue
            if c == "\r":
                if i + 1 < n and s[i + 1] == "\n":
                    i += 2
                else:
                    i += 1
                out.append("\\n")
                continue
            if c in ("\u2028", "\u2029"):
                out.append("\\n")
                i += 1
                continue
            if c == "\t":
                out.append("\\t")
                i += 1
                continue
            if o < 32:
                out.append(f"\\u{o:04x}")
                i += 1
                continue
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_string = True
        out.append(c)
        i += 1
    return "".join(out)


def _extract_first_balanced_object(s: str) -> str | None:
    """从首个 ``{`` 起按括号深度（忽略字符串内括号）截取完整 JSON 对象。"""
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(s)):
        c = s[i]
        if escape:
            escape = False
            continue
        if in_string:
            if c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1].strip()
    return None


def parse_json_object(raw: str) -> dict[str, Any]:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```\s*$", "", s)

    candidates: list[str] = []
    seen: set[str] = set()

    def add(x: str) -> None:
        x = x.strip()
        if x and x not in seen:
            seen.add(x)
            candidates.append(x)

    add(s)
    add(_sanitize_raw_chars_inside_json_strings(s))

    frag = _extract_first_balanced_object(s)
    if frag:
        add(frag)
        add(_sanitize_raw_chars_inside_json_strings(frag))

    start = s.find("{")
    end = s.rfind("}")
    if 0 <= start < end:
        frag2 = s[start : end + 1].strip()
        add(frag2)
        add(_sanitize_raw_chars_inside_json_strings(frag2))

    last_exc: json.JSONDecodeError | None = None
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError as e:
            last_exc = e
    if last_exc is not None:
        raise last_exc
    raise json.JSONDecodeError("无法解析 JSON 对象：输入为空或无任何大括号片段", raw or "", 0)
