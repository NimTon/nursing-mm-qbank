from __future__ import annotations

import json
import re
from typing import Any


def parse_json_object(raw: str) -> dict[str, Any]:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```\s*$", "", s)
    # 尝试直接解析；若失败则尽量从文本中截取第一个 JSON 对象片段再试。
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # 常见：网关未严格遵循 json_object，返回了前后说明文字/多段内容
        start = s.find("{")
        end = s.rfind("}")
        if 0 <= start < end:
            frag = s[start : end + 1].strip()
            return json.loads(frag)
        raise
