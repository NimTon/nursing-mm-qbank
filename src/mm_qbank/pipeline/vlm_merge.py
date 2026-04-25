from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any


_LEADING_NUM = re.compile(r"^\s*(\d{1,4})\s*[\.、\)\]】]?\s*")


def _题号排序键(s: str) -> tuple:
    t = s.strip()
    m = re.fullmatch(r"(\d+)", t)
    if m:
        return (0, int(m.group(1)), "")
    m2 = re.search(r"(\d+)", t)
    if m2:
        return (1, int(m2.group(1)), t)
    return (2, 0, t)


def merge_vlm_items_by_tihao(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """
    将 VLM ``items``（`题号` + `问题`/`解析` + `内容`）按题号分组合并
    成若干条，每条为 ``{题号, 问题, 解析}``（多段会换行连接）。
    """
    od: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    last_n: str | None = None
    for it in items:
        if not isinstance(it, dict):
            continue
        n = str(it.get("题号", "") or "").strip()
        key = n
        if key not in od:
            od[key] = {"题号": n, "问题": [], "解析": []}
        typ = str(it.get("类型", "")).strip()
        c = it.get("内容", "")
        if not isinstance(c, str):
            c = str(c) if c is not None else ""
        c = c.replace("\r\n", "\n").strip()
        if not c:
            continue
        # 兜底：当「题号」为空时，尝试从内容开头提取；再不行则把解析/问题归到上一题（按阅读顺序）。
        if not n:
            m = _LEADING_NUM.match(c)
            if m:
                n = m.group(1)
            elif last_n:
                n = last_n
        if n:
            last_n = n
        key = n
        if key not in od:
            od[key] = {"题号": n, "问题": [], "解析": []}
        if typ == "问题":
            od[key]["问题"].append(c)
        elif typ == "解析":
            od[key]["解析"].append(c)
        else:
            od[key]["问题"].append(c)
    out: list[dict[str, str]] = []
    for key in sorted(od.keys(), key=lambda k: _题号排序键(k) if k else (3, 0, "")):
        v = od[key]
        题号 = str(v.get("题号", "") or "")
        问题 = "\n\n".join(x for x in v["问题"] if x).strip()
        解析 = "\n\n".join(x for x in v["解析"] if x).strip()
        if not 问题 and not 解析:
            continue
        out.append({"题号": 题号, "问题": 问题, "解析": 解析})
    return out
