from __future__ import annotations

from typing import Any, Mapping


def qa_from_export_row(row: Mapping[str, Any]) -> tuple[str, str, str]:
    """
    从 refined / 结果 xlsx 行提取题号与「定稿」题干、解析文本：
    优先 ``修正后问题`` / ``修正后解析``，否则回退 ``原问题`` / ``原解析``，再回退 ``问题`` / ``解析``。
    """
    th = str(row.get("题号") or "").strip()
    修q = str(row.get("修正后问题") or "").strip()
    原q = str(row.get("原问题") or "").strip()
    问 = 修q or 原q or str(row.get("问题") or "").strip()
    修a = str(row.get("修正后解析") or "").strip()
    原a = str(row.get("原解析") or "").strip()
    析 = 修a or 原a or str(row.get("解析") or "").strip()
    t = th
    if not t and (问 or 析):
        t = "（空题号）"
    return (t, 问, 析)


def handout_stem_analysis(row: Mapping[str, Any]) -> tuple[str, str]:
    """
    讲义 Word 用：题目、解析仅展示「修正后」定稿；若未修正则回退原文（与 xlsx 定稿列语义一致）。
    """
    q = str(row.get("修正后问题") or "").strip() or str(row.get("原问题") or "").strip()
    a = str(row.get("修正后解析") or "").strip() or str(row.get("原解析") or "").strip()
    return (q, a)
