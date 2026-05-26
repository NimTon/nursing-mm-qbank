"""按题教材向修正（用于 scan → assemble 后的终步）。"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from mm_qbank.config import load_config, llm_text_settings, resolve_llm_model
from mm_qbank.io.csv_out import append_refined_rows_to_csv
from mm_qbank.io.xlsx_out import write_refined_rows_to_xlsx
from mm_qbank.llm.client import OpenAICompatClient
from mm_qbank.llm.jsonutil import parse_json_object
from mm_qbank.llm.retry import call_with_retries_timeout
from mm_qbank.pipeline.scan_common import cap_workers
from mm_qbank.prompts_loader import refine_system, refine_user_payload

_log = logging.getLogger(__name__)


def _text_norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _strip_wrapping_artifacts(s: str) -> str:
    t = s.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines)
    return t.rstrip()


def _parse_refine_response(raw: str) -> list[dict[str, Any]]:
    data = parse_json_object(_strip_wrapping_artifacts(raw))
    it = data.get("items")
    if not isinstance(it, list):
        return []
    return [x for x in it if isinstance(x, dict)]


def _normalize_out_item(m: dict[str, str], raw: dict[str, Any]) -> dict[str, Any]:
    题 = m.get("题号", "")
    题型 = str(m.get("题目类型") or "").strip() or "未知"
    原问 = m.get("问题", "")
    原析 = m.get("解析", "")
    修q_raw = str(raw.get("修正后问题", "") or "").strip()
    修a_raw = str(raw.get("修正后解析", "") or "").strip()
    修q = 修q_raw or 原问
    修a = 修a_raw or 原析
    changed = _text_norm(修q) != _text_norm(原问) or _text_norm(修a) != _text_norm(原析)
    qt_raw = str(raw.get("题目类型") or "").strip()
    题型 = qt_raw or 题型
    return {
        "题号": str(raw.get("题号", 题) or 题).strip() or 题,
        "题目类型": 题型,
        "原问题": 原问,
        "原解析": 原析,
        "修正后问题": 修q_raw if changed else "",
        "修正问题原因": (str(raw.get("修正问题原因", "") or "").strip() if changed else ""),
        "修正问题参考来源": (str(raw.get("修正问题参考来源", "") or "").strip() if changed else ""),
        "修正后解析": 修a_raw if changed else "",
        "修正解析原因": (str(raw.get("修正解析原因", "") or "").strip() if changed else ""),
        "修正解析参考来源": (str(raw.get("修正解析参考来源", "") or "").strip() if changed else ""),
        "修正状态": changed,
    }


def _row_to_merge_input(row: dict[str, Any]) -> dict[str, str]:
    q = str(row.get("原问题") or row.get("问题") or "").strip()
    a = str(row.get("原解析") or row.get("解析") or "").strip()
    return {
        "题号": str(row.get("题号") or "").strip(),
        "题目类型": str(row.get("题目类型") or "").strip() or "未知",
        "问题": q,
        "解析": a,
    }


def run_refine_on_rows(
    rows: list[dict[str, Any]],
    *,
    out_xlsx: Path,
    out_csv: Path | None = None,
    out_jsonl: Path | None = None,
    config_path: Path | None = None,
    model: str | None = None,
    max_workers: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """对 assemble 后的题目逐题调用教材向修正 LLM（无卷面解析时由模型生成）。"""
    cfg = load_config(config_path)
    tl = llm_text_settings()
    if not tl.get("api_key"):
        raise ValueError("未配置 LLM_API_KEY")
    rcfg = dict(cfg.get("refine") or {})
    text_model = resolve_llm_model(cfg, override=model)
    temp = float(rcfg.get("temperature", 0.2))
    timeout = float(rcfg.get("timeout_seconds", 300.0))
    web_search = bool(rcfg.get("web_search", False))
    default_headers = {"X-Web-Search": "true"} if web_search else None
    rsys = refine_system(include_lecture_tips=False)

    inputs = [_row_to_merge_input(r) for r in rows if str(r.get("题号") or r.get("原问题") or "").strip()]
    if not inputs:
        raise ValueError("无可修正题目")

    cout = (out_csv or out_xlsx.with_name(out_xlsx.stem + "_stream.csv")).resolve()
    jout = (out_jsonl or out_xlsx.with_name(out_xlsx.stem + ".jsonl")).resolve()
    xout = out_xlsx.resolve()
    cout.parent.mkdir(parents=True, exist_ok=True)

    raw_max = max_workers if max_workers is not None else rcfg.get("max_workers")
    n_tasks = len(inputs)
    max_rw = cap_workers(n_tasks, raw_max)
    _log.info("教材修正：%s 题，并行 max_workers=%s", n_tasks, max_rw)

    csv_lock = Lock()
    prog_lock = Lock()
    n_done = 0
    flat: list[dict[str, Any]] = []

    def _refine_one(idx: int, m: dict[str, str]) -> dict[str, Any]:
        chunk = [m]
        up = refine_user_payload(
            page_id=f"scan-assemble {idx}",
            merged=chunk,
            web_search=web_search,
            include_lecture_tips=False,
        )
        cl = OpenAICompatClient(
            api_key=str(tl.get("api_key")),
            base_url=tl.get("base_url") or None,
            default_headers=default_headers,
        )

        def _call_once(tout: float) -> str:
            try:
                return cl.chat_text_json(
                    model=text_model,
                    messages=[
                        {"role": "system", "content": rsys},
                        {"role": "user", "content": up},
                    ],
                    temperature=temp,
                    timeout=float(tout),
                )
            except Exception:  # noqa: BLE001
                return cl.chat_text_json(
                    model=text_model,
                    messages=[
                        {"role": "system", "content": rsys},
                        {"role": "user", "content": up},
                    ],
                    temperature=temp,
                    timeout=None,
                )

        content = call_with_retries_timeout(
            _call_once,
            tries=3,
            base_timeout_s=float(timeout),
            base_sleep_s=1.0,
        )
        out_items = _parse_refine_response(content)
        if not out_items:
            return _normalize_out_item(m, {"题号": m["题号"], "题目类型": m["题目类型"]})
        return _normalize_out_item(m, out_items[0])

    def _after_one(row: dict[str, Any]) -> None:
        nonlocal n_done
        with csv_lock:
            append_refined_rows_to_csv(
                cout,
                [row],
                include_lecture_tips=False,
                include_lecture_content=False,
            )
        if on_progress:
            with prog_lock:
                n_done += 1
                nd = n_done
            try:
                on_progress(nd, n_tasks)
            except Exception:  # noqa: BLE001
                pass

    if max_rw <= 1:
        for i, m in enumerate(inputs, start=1):
            row = _refine_one(i, m)
            flat.append(row)
            _after_one(row)
    else:
        with ThreadPoolExecutor(max_workers=max_rw) as ex:
            futs = {ex.submit(_refine_one, i, m): i for i, m in enumerate(inputs, start=1)}
            parts: list[tuple[int, dict[str, Any]]] = []
            for fut in as_completed(futs):
                parts.append((futs[fut], fut.result()))
            for _, row in sorted(parts, key=lambda t: t[0]):
                flat.append(row)
                _after_one(row)

    jout.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in flat) + ("\n" if flat else ""),
        encoding="utf-8",
    )
    write_refined_rows_to_xlsx(
        xout,
        flat,
        include_lecture_tips=False,
        include_lecture_content=False,
    )
    return {
        "n_rows": len(flat),
        "out_jsonl": str(jout),
        "out_csv": str(cout),
        "out_xlsx": str(xout),
        "model": text_model,
        "web_search": web_search,
    }
