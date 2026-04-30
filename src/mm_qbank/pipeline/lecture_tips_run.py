from __future__ import annotations

import json
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any, cast

import csv

from mm_qbank.config import load_config, llm_text_settings
from mm_qbank.io.xlsx_lecture import (
    append_lecture_tips_rows_to_csv,
    load_done_行号_from_lecture_csv,
    read_result_xlsx,
    write_lecture_tips_xlsx,
)
from mm_qbank.llm.client import OpenAICompatClient
from mm_qbank.llm.jsonutil import parse_json_object
from mm_qbank.llm.retry import call_with_retries_timeout
from mm_qbank.prompts_loader import lecture_tips_system, lecture_tips_user_payload

_log = logging.getLogger(__name__)


def _cell_qa(row: dict[str, str]) -> tuple[str, str, str]:
    th = (row.get("题号") or "").strip()
    修q = (row.get("修正后问题") or "").strip()
    原q = (row.get("原问题") or "").strip()
    问 = 修q or 原q or (row.get("问题") or "").strip()
    修a = (row.get("修正后解析") or "").strip()
    原a = (row.get("原解析") or "").strip()
    析 = 修a or 原a or (row.get("解析") or "").strip()
    t = th
    if not t and (问 or 析):
        t = "（空题号）"
    return (t, 问, 析)


def _build_llm_items(
    batch: list[tuple[int, dict[str, str]]],
) -> list[dict[str, str | int]]:
    out: list[dict[str, str | int]] = []
    for excel_row, d in batch:
        t, q, a = _cell_qa(d)
        out.append(
            {
                "行号": int(excel_row),
                "题号": t,
                "问题": q,
                "解析": a,
            }
        )
    return out


def _parse_lecture_response(raw: str) -> list[dict[str, Any]]:
    s = raw.strip()
    if s.startswith("```"):
        lines = s.split("\n")
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        s = "\n".join(lines)
    data = parse_json_object(s)
    it = data.get("items")
    if not isinstance(it, list):
        return []
    return [cast(dict[str, Any], x) for x in it if isinstance(x, dict)]


def _cap_workers(n_tasks: int, raw: Any) -> int:
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        w = min(8, (os.cpu_count() or 4) * 2)
    else:
        w = int(raw)
    w = max(1, w)
    return min(w, max(1, n_tasks))


def _merge_tips_to_rows(
    headers: list[str],
    data: list[tuple[int, dict[str, str]]],
    tips: dict[int, str],
) -> tuple[list[str], list[dict[str, str]]]:
    h2 = list(headers)
    if "讲师提醒" not in h2:
        h2.append("讲师提醒")
    rows: list[dict[str, str]] = []
    for rnum, d in data:
        out: dict[str, str] = {k: (d.get(k) or "") for k in h2 if k != "讲师提醒"}
        out["讲师提醒"] = str(tips.get(int(rnum), d.get("讲师提醒") or ""))
        rows.append({k: out.get(k, "") for k in h2})
    return h2, rows


def run_lecture_tips_from_xlsx(
    *,
    in_xlsx: Path,
    out_xlsx: Path | None = None,
    out_csv: Path | None = None,
    out_jsonl: Path | None = None,
    config_path: Path | None = None,
    model: str | None = None,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """
    从「结果」xlsx（与 refined 表结构兼容）读入，按行调用 LLM 写「讲师提醒」字段，流式追加 CSV 断点续跑，再另存带新列的 xlsx。

    仅当每批成功追加 CSV 后才会更新断点与内存；jsonl 为**整张表**每行最终「讲师提醒」汇总（与本次新跑 LLM 条数可不等）。
    """
    cfg = load_config(config_path)
    tl = llm_text_settings()
    if not tl.get("api_key"):
        raise ValueError("未配置 LLM_API_KEY：请在 .env 中设置 LLM_BASE_URL 与 LLM_API_KEY")

    lcfg = dict(cfg.get("lecture_tips") or {})
    rcfg = dict(cfg.get("refine") or {})
    text_model = (model or "").strip() or str(
        (lcfg.get("model") or rcfg.get("model") or tl.get("text_model") or "gpt-4o-mini")
    )
    web_search = bool(lcfg.get("web_search", False))
    temp = float(lcfg.get("temperature", 0.35))
    timeout = float(
        lcfg.get("timeout_seconds", rcfg.get("timeout_seconds", 180.0) or 180.0) or 180.0
    )
    batch_size = int(lcfg.get("batch_size", rcfg.get("batch_size", 10)) or 10)
    if batch_size < 1:
        batch_size = 10

    default_headers: dict[str, str] | None = None
    burl = (tl.get("base_url") or "") or ""
    if web_search and "dashscope" in burl.lower():
        default_headers = {"X-DashScope-Enable-Internet-Search": "enable"}

    src = in_xlsx.resolve()
    if not src.is_file():
        raise FileNotFoundError(f"未找到 xlsx: {src}")

    base = src.stem
    _dir = src.parent
    cout = (out_csv or (_dir / f"{base}_lecture_tips_stream.csv")).resolve()
    xout = (out_xlsx or (_dir / f"{base}_lecture_tips.xlsx")).resolve()
    jout = (out_jsonl or (_dir / f"{base}_lecture_tips.jsonl")).resolve()

    done_rows = load_done_行号_from_lecture_csv(cout)
    if done_rows:
        _log.info("检测到已存在流式 CSV，将跳过行数=%s -> %s", len(done_rows), str(cout))

    tips_map: dict[int, str] = {}
    if cout.exists():
        try:
            with cout.open("r", encoding="utf-8-sig", newline="") as f:
                rdr = csv.DictReader(f)
                for row in rdr:
                    if not row:
                        continue
                    s = str(row.get("行号", "") or "").strip()
                    if s.isdigit():
                        tips_map[int(s)] = str(row.get("讲师提醒", "") or "")
        except OSError:  # noqa: BLE001
            pass

    headers, data = read_result_xlsx(src)
    if not data:
        raise ValueError("xlsx 中无数据行（或表为空）")

    work: list[tuple[int, dict[str, str]]] = []
    for excel_row, d in data:
        t, q, a = _cell_qa(d)
        if int(excel_row) in done_rows:
            continue
        if not (t or q or a):
            continue
        work.append((excel_row, d))

    lsys = lecture_tips_system()
    total = len(work)
    flat_log: list[dict[str, Any]] = []
    n_batches = 0

    if total > 0:
        raw_max_workers = lcfg.get("max_workers", rcfg.get("max_workers"))
        if total <= batch_size:
            target_tasks = _cap_workers(total, raw_max_workers)
            chunk_size = int(math.ceil(total / target_tasks)) if target_tasks > 0 else total
        else:
            chunk_size = batch_size
        batches: list[tuple[int, int, int]] = []
        for batch_idx, st in enumerate(range(0, total, chunk_size), start=1):
            ed = min(total, st + chunk_size)
            batches.append((batch_idx, st, ed))
        n_batches = len(batches)
        max_rw = _cap_workers(n_batches, raw_max_workers)
        _log.info(
            "lecture-tips 待处理行=%s 批次数=%s chunk_size=%s batch_size=%s max_workers=%s",
            total,
            n_batches,
            chunk_size,
            batch_size,
            max_rw,
        )
        csv_lock = Lock()
        prog_lock = Lock()
        n_done = 0

        def _refine_one(bix: int, st: int, ed: int) -> int:
            nonlocal n_done
            chunk = work[st:ed]
            items = _build_llm_items(chunk)
            c = OpenAICompatClient(
                api_key=str(tl.get("api_key")),
                base_url=tl.get("base_url") or None,
                default_headers=default_headers,
            )
            up = lecture_tips_user_payload(
                batch_id=f"{Path(src).name} #{bix}/{n_batches}",
                items=items,
                web_search=web_search,
            )
            _log.info("[%s/%s] 讲师提醒 batch 行数=%s", bix, n_batches, len(items))

            def _call_once(tout: float) -> str:
                try:
                    return c.chat_text_json(
                        model=text_model,
                        messages=[
                            {"role": "system", "content": lsys},
                            {"role": "user", "content": up},
                        ],
                        temperature=temp,
                        timeout=float(tout),
                    )
                except Exception:  # noqa: BLE001
                    return c.chat_text_json(
                        model=text_model,
                        messages=[
                            {"role": "system", "content": lsys},
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
                on_retry=lambda att, e, s, nt: _log.warning(
                    "lecture-tips LLM 重试 batch=%s/%s att=%s/3 sleep=%.2fs err=%s: %s",
                    bix,
                    n_batches,
                    att + 1,
                    s,
                    type(e).__name__,
                    e,
                ),
            )
            parsed = _parse_lecture_response(content)
            by_row: dict[int, str] = {}
            for it in parsed:
                rkey = it.get("行号")
                try:
                    rnum = int(rkey)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
                tip = str(it.get("讲师提醒", "") or "").strip()
                by_row[int(rnum)] = tip
            if len(chunk) == len(items) and len(parsed) < len(items):
                _log.warning("batch %s: 返回条数=%s 少于输入=%s，缺行将填空", bix, len(parsed), len(items))
            flat_csv: list[dict[str, Any]] = []
            for (exr, d) in chunk:
                tcell, _, _ = _cell_qa(d)
                tip = by_row.get(int(exr), "")
                rec = {
                    "行号": int(exr),
                    "题号": tcell,
                    "讲师提醒": tip,
                }
                flat_csv.append(rec)
            try:
                with csv_lock:
                    append_lecture_tips_rows_to_csv(cout, flat_csv)
            except Exception as e:  # noqa: BLE001
                _log.warning("写流式 CSV 失败（本批未计入断点与最终合并，可重试）: %s", e)
            else:
                for rec in flat_csv:
                    tips_map[int(rec["行号"])] = str(rec.get("讲师提醒", "") or "")
                for _ in flat_csv:
                    with prog_lock:
                        n_done += 1
                    if on_progress is not None:
                        try:
                            on_progress(n_done, total)
                        except Exception:  # noqa: BLE001
                            pass
                for r in flat_csv:
                    flat_log.append(cast(dict[str, Any], r))
            return st

        if max_rw <= 1 or n_batches <= 1:
            for bix, st, ed in batches:
                _refine_one(bix, st, ed)
        else:
            with ThreadPoolExecutor(max_workers=max_rw) as ex:
                futs = [ex.submit(_refine_one, bix, st, ed) for bix, st, ed in batches]
                for f in as_completed(futs):
                    f.result()
    h_final, x_rows = _merge_tips_to_rows(headers, data, tips_map)
    jout.parent.mkdir(parents=True, exist_ok=True)
    j_full: list[dict[str, Any]] = []
    for exr, d in data:
        t, _, _ = _cell_qa(d)
        tip_final = tips_map.get(int(exr), str(d.get("讲师提醒", "") or ""))
        j_full.append({"行号": exr, "题号": t, "讲师提醒": tip_final})
    jout.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in j_full) + ("\n" if j_full else ""),
        encoding="utf-8",
    )
    write_lecture_tips_xlsx(xout, h_final, x_rows)
    return {
        "n_pending_rows": total,
        "n_stream_rows": len(flat_log),
        "n_batches": n_batches,
        "out_xlsx": str(xout),
        "out_csv": str(cout),
        "out_jsonl": str(jout),
        "model": text_model,
        "web_search": web_search,
        "in_xlsx": str(src),
    }
