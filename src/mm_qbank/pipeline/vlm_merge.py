from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any

_LEADING_NUM = re.compile(r"^\s*(\d{1,4})\s*[\.、\)\]】]?\s*")

# 聚合/断点续跑用：题号 + 题型（避免「单选第10题」与「多选第10题」共用题号键而误合并）
_AGG_BUCKET_SEP = "\x1f"


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


def aggregate_bucket_key(tihao: str, kind: str) -> str:
    """
    跨页/缓冲聚合用的稳定键：同一卷面题号在不同大题（单选/多选）下会重复，必须与题型一起区分。
    ``题号`` 仍对用户展示为数字；本键仅用于内部 dict 与 CSV 断点匹配。
    """
    th = (tihao or "").strip()
    k = (kind or "").strip()
    kn = _normalize_question_kind_label(k) if k else "未知"
    return f"{th}{_AGG_BUCKET_SEP}{kn}"


def parse_aggregate_bucket_key(key: str) -> tuple[str, str]:
    """``aggregate_bucket_key`` 的逆操作；无分隔符时题型视为「未知」。"""
    if _AGG_BUCKET_SEP in key:
        a, b = key.split(_AGG_BUCKET_SEP, 1)
        return a, b
    return (key or "").strip(), "未知"


def row_aggregate_key(row: dict[str, Any]) -> str:
    """结果表一行 → 聚合键（题号 + 题目类型）。"""
    th = str(row.get("题号") or "").strip()
    if not th:
        return ""
    qt = str(row.get("题目类型") or "").strip()
    return aggregate_bucket_key(th, qt or "未知")


def merge_sort_key_for_aggregate_bucket(bucket_key: str) -> tuple:
    """``agg`` / ``buf`` 等以 ``aggregate_bucket_key`` 为键时的排序键。"""
    th, kn = parse_aggregate_bucket_key(bucket_key)
    kr = _normalize_question_kind_label(kn) if kn else "未知"
    return (question_kind_rank(kr), _题号排序键(th))


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


def _pick_bucket_key(
    n: str,
    typ: str,
    qt_raw: str,
    od: "OrderedDict[str, dict[str, Any]]",
    last_bucket_for_n: dict[str, str],
) -> str:
    if qt_raw.strip():
        bucket_key = aggregate_bucket_key(n, qt_raw)
        if n:
            last_bucket_for_n[n] = bucket_key
        return bucket_key
    if typ == "解析" and n:
        candidates = [
            k
            for k in od
            if parse_aggregate_bucket_key(k)[0] == n
            and od[k]["问题"]
            and not od[k]["解析"]
        ]
        if len(candidates) == 1:
            bucket_key = candidates[0]
            if n:
                last_bucket_for_n[n] = bucket_key
            return bucket_key
    if n and n in last_bucket_for_n:
        return last_bucket_for_n[n]
    if n:
        bucket_key = aggregate_bucket_key(n, "未知")
        last_bucket_for_n[n] = bucket_key
        return bucket_key
    return aggregate_bucket_key("", "未知")


def merge_vlm_items_by_tihao(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    """
    将 VLM ``items``（`题号` + `类型`(问题/解析) + `内容`；可选 `题目类型`）按 **题号+题目类型** 分组合并
    成若干条，每条为 ``{题号, 题目类型, 问题, 解析}``（多段会换行连接）。

    同一题号下「单选 / 多选」等不得合并到同一条，避免误触发后续按 ``10-1`` 形式拆分。
    """
    od: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    last_n: str | None = None
    last_bucket_for_n: dict[str, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        n = str(it.get("题号", "") or "").strip()
        typ = str(it.get("类型", "")).strip()
        qt_raw = str(it.get("题目类型") or it.get("题型") or it.get("试题类型") or "").strip()
        c = it.get("内容", "")
        if not isinstance(c, str):
            c = str(c) if c is not None else ""
        c = c.replace("\r\n", "\n").strip()
        if c:
            if not n:
                m = _LEADING_NUM.match(c)
                if m:
                    n = m.group(1)
                elif last_n:
                    n = last_n
            if n:
                last_n = n
        if not c:
            continue
        if not n and last_n:
            n = last_n
        if n:
            last_n = n
        bucket_key = _pick_bucket_key(n, typ, qt_raw, od, last_bucket_for_n)

        if bucket_key not in od:
            od[bucket_key] = {"题号": n, "问题": [], "解析": [], "题目类型": ""}
        if qt_raw and not str(od[bucket_key].get("题目类型") or "").strip():
            od[bucket_key]["题目类型"] = qt_raw
        if typ == "问题":
            od[bucket_key]["问题"].append(c)
        elif typ == "解析":
            od[bucket_key]["解析"].append(c)
        else:
            od[bucket_key]["问题"].append(c)
    out: list[dict[str, str]] = []
    for bucket_key in sorted(od.keys(), key=merge_sort_key_for_aggregate_bucket):
        v = od[bucket_key]
        题号 = str(v.get("题号", "") or "")
        问题 = "\n\n".join(x for x in v["问题"] if x).strip()
        解析 = "\n\n".join(x for x in v["解析"] if x).strip()
        if not 问题 and not 解析:
            continue
        qt_out = str(v.get("题目类型") or "").strip() or "未知"
        out.append({"题号": 题号, "题目类型": qt_out, "问题": 问题, "解析": 解析})
    return out
