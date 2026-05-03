from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any

_LEADING_NUM = re.compile(r"^\s*(\d{1,4})\s*[\.、\)\]】]?\s*")

# 导出/展示顺序：题型序号越小越靠前（同类再按题号）
_KIND_ORDER: dict[str, int] = {
    "单选": 10,
    "单选题": 10,
    "多选": 20,
    "多选题": 20,
    "判断": 30,
    "判断题": 30,
    "填空": 40,
    "填空题": 40,
    "简答": 50,
    "简答题": 50,
    "名词解释": 60,
    "论述": 65,
    "论述题": 65,
    "案例分析": 70,
    "病例分析": 70,
    "病例分析题": 70,
    "未知": 9990,
    "其它": 9990,
}


def _normalize_question_kind_label(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return "未知"
    if s in _KIND_ORDER:
        return s
    if s.endswith("题") and len(s) >= 2:
        base = s[:-1]
        if base in _KIND_ORDER:
            return base
    return s


def question_kind_rank(label: str) -> int:
    """题型排序权重（越小越靠前）；无法识别时靠后但不压过「未知」占位。"""
    n = _normalize_question_kind_label(label)
    return _KIND_ORDER.get(n, 8999)


def _题号排序键(s: str) -> tuple:
    t = s.strip()
    m = re.fullmatch(r"(\d+)", t)
    if m:
        return (0, int(m.group(1)), "")
    m2 = re.search(r"(\d+)", t)
    if m2:
        return (1, int(m2.group(1)), t)
    return (2, 0, t)


def merge_sort_key_for_row(row: dict[str, Any]) -> tuple:
    """精修结果表一行：先按题型，再按题号（用于 jsonl/xlsx 最终排序）。"""
    qt = str(row.get("题目类型") or "").strip()
    th = str(row.get("题号") or "").strip()
    return (question_kind_rank(qt), _题号排序键(th))


def merge_sort_key_tihao_and_kind(tihao: str, kind: str) -> tuple:
    """聚合缓冲 ``agg`` 键排序：题型 → 题号。"""
    return (question_kind_rank(kind), _题号排序键(tihao))


def merge_vlm_items_by_tihao(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """
    将 VLM ``items``（`题号` + `类型`(问题/解析) + `内容`；可选 `题目类型`）按题号分组合并
    成若干条，每条为 ``{题号, 题目类型, 问题, 解析}``（多段会换行连接）。
    """
    od: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    last_n: str | None = None
    for it in items:
        if not isinstance(it, dict):
            continue
        n = str(it.get("题号", "") or "").strip()
        key = n
        if key not in od:
            od[key] = {"题号": n, "问题": [], "解析": [], "题目类型": ""}
        typ = str(it.get("类型", "")).strip()
        qt_raw = str(it.get("题目类型") or it.get("题型") or it.get("试题类型") or "").strip()
        if qt_raw and not str(od[key].get("题目类型") or "").strip():
            od[key]["题目类型"] = qt_raw
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
            od[key] = {"题号": n, "问题": [], "解析": [], "题目类型": ""}
        if qt_raw and not str(od[key].get("题目类型") or "").strip():
            od[key]["题目类型"] = qt_raw
        if typ == "问题":
            od[key]["问题"].append(c)
        elif typ == "解析":
            od[key]["解析"].append(c)
        else:
            od[key]["问题"].append(c)
    out: list[dict[str, str]] = []
    for key in sorted(
        od.keys(),
        key=lambda k: (
            question_kind_rank(str(od[k].get("题目类型") or "")),
            _题号排序键(k) if k else (3, 0, ""),
        ),
    ):
        v = od[key]
        题号 = str(v.get("题号", "") or "")
        问题 = "\n\n".join(x for x in v["问题"] if x).strip()
        解析 = "\n\n".join(x for x in v["解析"] if x).strip()
        if not 问题 and not 解析:
            continue
        qt_out = str(v.get("题目类型") or "").strip() or "未知"
        out.append({"题号": 题号, "题目类型": qt_out, "问题": 问题, "解析": 解析})
    return out
