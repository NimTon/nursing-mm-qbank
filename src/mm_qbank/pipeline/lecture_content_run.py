from __future__ import annotations

import csv
import json
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock
from typing import Any, cast

from mm_qbank.config import load_config, llm_text_settings
from mm_qbank.io.docx_out import write_lecture_handout_docx
from mm_qbank.io.result_row_qa import qa_from_export_row
from mm_qbank.io.xlsx_out import write_refined_rows_to_xlsx
from mm_qbank.io.xlsx_lecture import read_result_xlsx, write_lecture_tips_xlsx
from mm_qbank.llm.client import OpenAICompatClient
from mm_qbank.llm.jsonutil import parse_json_object
from mm_qbank.llm.retry import call_with_retries_timeout
from mm_qbank.prompts_loader import (
    lecture_content_system,
    lecture_content_user_payload_by_row,
    lecture_content_user_payload_by_tihao,
)

_log = logging.getLogger(__name__)

_STREAM_BY_TIHAO: tuple[str, ...] = ("题号", "要点", "讲课内容")
_STREAM_BY_ROW: tuple[str, ...] = ("行号", "题号", "要点", "讲课内容")


def _cap_workers(n_tasks: int, raw: Any) -> int:
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        w = min(8, (os.cpu_count() or 4) * 2)
    else:
        w = int(raw)
    w = max(1, w)
    return min(w, max(1, n_tasks))


def _parse_items_json(raw: str) -> list[dict[str, Any]]:
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


def append_lecture_content_tihao_csv(path: Path, rows: list[dict[str, Any]]) -> int:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = _STREAM_BY_TIHAO
    needs_header = not path.exists() or path.stat().st_size <= 0
    w = 0
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(columns), extrasaction="ignore")
        if needs_header:
            wr.writeheader()
        for row in rows:
            o = {k: ("" if row.get(k) is None else str(row.get(k) or "")) for k in columns}
            wr.writerow(o)
            w += 1
        f.flush()
    return w


def load_done_tihao_from_lecture_content_csv(path: Path) -> set[str]:
    """仅当「要点」「讲课内容」均非空时视为该行已成功，可跳过 LLM。"""
    p = path.resolve()
    if not p.exists():
        return set()
    done: set[str] = set()
    try:
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                if not row:
                    continue
                t = str(row.get("题号", "") or "").strip()
                pt = str(row.get("要点", "") or "").strip()
                lc = str(row.get("讲课内容", "") or "").strip()
                if t and pt and lc:
                    done.add(t)
    except OSError:
        return set()
    return done


def append_lecture_content_row_csv(path: Path, rows: list[dict[str, Any]]) -> int:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = _STREAM_BY_ROW
    needs_header = not path.exists() or path.stat().st_size <= 0
    w = 0
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(columns), extrasaction="ignore")
        if needs_header:
            wr.writeheader()
        for row in rows:
            o = {k: ("" if row.get(k) is None else str(row.get(k) or "")) for k in columns}
            wr.writerow(o)
            w += 1
        f.flush()
    return w


def load_done_rownums_from_lecture_content_csv(path: Path) -> set[int]:
    """仅当「要点」「讲课内容」均非空时视为该行已成功。"""
    p = path.resolve()
    if not p.exists():
        return set()
    done: set[int] = set()
    try:
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                if not row:
                    continue
                s = str(row.get("行号", "") or "").strip()
                pt = str(row.get("要点", "") or "").strip()
                lc = str(row.get("讲课内容", "") or "").strip()
                if s.isdigit() and pt and lc:
                    done.add(int(s))
    except OSError:
        return set()
    return done


def _load_lc_map_from_tihao_csv(path: Path) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    if not path.exists():
        return out
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                if not row:
                    continue
                th = str(row.get("题号", "") or "").strip()
                if not th:
                    continue
                out[th] = (
                    str(row.get("要点", "") or ""),
                    str(row.get("讲课内容", "") or ""),
                )
    except OSError:
        pass
    return out


def _load_lc_map_from_row_csv(path: Path) -> dict[int, tuple[str, str, str]]:
    """行号 -> (题号, 要点, 讲课内容)"""
    out: dict[int, tuple[str, str, str]] = {}
    if not path.exists():
        return out
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            r = csv.DictReader(f)
            for row in r:
                if not row:
                    continue
                s = str(row.get("行号", "") or "").strip()
                if not s.isdigit():
                    continue
                rn = int(s)
                out[rn] = (
                    str(row.get("题号", "") or ""),
                    str(row.get("要点", "") or ""),
                    str(row.get("讲课内容", "") or ""),
                )
    except OSError:
        pass
    return out


def _merge_lc_into_refined_rows(
    rows: list[dict[str, Any]], by_tihao: dict[str, tuple[str, str]]
) -> None:
    for row in rows:
        th = str(row.get("题号") or "").strip()
        pt, lc = by_tihao.get(th, ("", ""))
        row["要点"] = pt
        row["讲课内容"] = lc


def _merge_lc_xlsx_rows(
    headers: list[str],
    data: list[tuple[int, dict[str, str]]],
    by_row: dict[int, tuple[str, str, str]],
) -> tuple[list[str], list[dict[str, str]]]:
    h2 = list(headers)
    for col in ("要点", "讲课内容"):
        if col not in h2:
            h2.append(col)
    out_rows: list[dict[str, str]] = []
    for rnum, d in data:
        row = {k: (d.get(k) or "") for k in h2}
        ir = int(rnum)
        if ir in by_row:
            _, pt, lc = by_row[ir]
        else:
            pt = str(d.get("要点") or "")
            lc = str(d.get("讲课内容") or "")
        row["要点"] = pt
        row["讲课内容"] = lc
        out_rows.append({k: row.get(k, "") for k in h2})
    return h2, out_rows


def run_lecture_content_on_refined_rows(
    rows: list[dict[str, Any]],
    *,
    out_xlsx: Path,
    out_csv: Path | None = None,
    out_docx: Path | None = None,
    out_jsonl: Path | None = None,
    config_path: Path | None = None,
    model: str | None = None,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """
    对已修正的行列表写入「要点」「讲课内容」，覆盖保存 ``out_xlsx``，并生成 ``_讲课稿.docx``。
    流式 CSV 按 ``题号`` 断点续跑。
    """
    cfg = load_config(config_path)
    tl = llm_text_settings()
    if not tl.get("api_key"):
        raise ValueError("未配置 LLM_API_KEY：请在 .env 中设置 LLM_BASE_URL 与 LLM_API_KEY")

    lcfg = dict(cfg.get("lecture_content") or {})
    rcfg = dict(cfg.get("refine") or {})
    text_model = (model or "").strip() or str(
        (lcfg.get("model") or rcfg.get("model") or tl.get("text_model") or "gpt-4o-mini")
    )
    web_search = bool(lcfg.get("web_search", False))
    temp = float(lcfg.get("temperature", 0.4))
    timeout = float(
        lcfg.get("timeout_seconds", rcfg.get("timeout_seconds", 300.0) or 300.0) or 300.0
    )
    batch_size = int(lcfg.get("batch_size", 1) or 1)
    if batch_size < 1:
        batch_size = 1

    default_headers: dict[str, str] | None = None
    burl = (tl.get("base_url") or "") or ""
    if web_search and "dashscope" in burl.lower():
        default_headers = {"X-DashScope-Enable-Internet-Search": "enable"}

    xout = out_xlsx.resolve()
    base = xout.stem
    _dir = xout.parent
    cout = (out_csv or (_dir / f"{base}_lecture_content_stream.csv")).resolve()
    dout = (out_docx or (_dir / f"{base}_讲课稿.docx")).resolve()
    jout = (out_jsonl or (_dir / f"{base}_lecture_content.jsonl")).resolve()

    complete_tihao = load_done_tihao_from_lecture_content_csv(cout)
    by_tihao = _load_lc_map_from_tihao_csv(cout)

    for row in rows:
        th = str(row.get("题号") or "").strip()
        if not th:
            continue
        if th in by_tihao:
            continue
        p0 = str(row.get("要点") or "").strip()
        l0 = str(row.get("讲课内容") or "").strip()
        if p0 or l0:
            by_tihao[th] = (p0, l0)

    work: list[dict[str, Any]] = []
    for row in rows:
        th = str(row.get("题号") or "").strip()
        _, q, a = qa_from_export_row(row)
        if not th and not (q or a):
            continue
        if th and th in complete_tihao:
            continue
        if th:
            pt0, lc0 = by_tihao.get(th, ("", ""))
            if pt0.strip() and lc0.strip():
                continue
        if not (q or a):
            continue
        work.append(row)

    lsys = lecture_content_system()
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
            "lecture-content(refined) 待处理题=%s 批次数=%s chunk=%s batch_size=%s max_workers=%s",
            total,
            n_batches,
            chunk_size,
            batch_size,
            max_rw,
        )
        csv_lock = Lock()
        prog_lock = Lock()
        n_done = 0

        def _one_batch(bix: int, st: int, ed: int) -> None:
            nonlocal n_done
            chunk = work[st:ed]
            items: list[dict[str, str]] = []
            for row in chunk:
                th, q, a = qa_from_export_row(row)
                items.append({"题号": th, "问题": q, "解析": a})
            c = OpenAICompatClient(
                api_key=str(tl.get("api_key")),
                base_url=tl.get("base_url") or None,
                default_headers=default_headers,
            )
            up = lecture_content_user_payload_by_tihao(
                batch_id=f"{Path(xout).name} #{bix}/{n_batches}",
                items=items,
                web_search=web_search,
            )

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
                    "lecture-content LLM 重试 batch=%s/%s att=%s/3 sleep=%.2fs err=%s: %s",
                    bix,
                    n_batches,
                    att + 1,
                    s,
                    type(e).__name__,
                    e,
                ),
            )
            parsed = _parse_items_json(content)
            by_out: dict[str, tuple[str, str]] = {}
            for it in parsed:
                tk = str(it.get("题号", "") or "").strip()
                if not tk:
                    continue
                by_out[tk] = (
                    str(it.get("要点", "") or "").strip(),
                    str(it.get("讲课内容", "") or "").strip(),
                )
            if len(chunk) == len(items) and len(parsed) < len(items):
                _log.warning("batch %s: 返回条数=%s 少于输入=%s", bix, len(parsed), len(items))

            flat_csv: list[dict[str, Any]] = []
            for row in chunk:
                th, _, _ = qa_from_export_row(row)
                pt, lc = by_out.get(th, ("", ""))
                flat_csv.append({"题号": th, "要点": pt, "讲课内容": lc})
            try:
                with csv_lock:
                    append_lecture_content_tihao_csv(cout, flat_csv)
            except Exception as e:  # noqa: BLE001
                _log.warning("写讲课内容 CSV 失败: %s", e)
            else:
                for rec in flat_csv:
                    thk = str(rec.get("题号", "") or "").strip()
                    if thk:
                        ptk = str(rec.get("要点", "") or "").strip()
                        ltk = str(rec.get("讲课内容", "") or "").strip()
                        by_tihao[thk] = (ptk, ltk)
                        if ptk and ltk:
                            complete_tihao.add(thk)
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

        if max_rw <= 1 or n_batches <= 1:
            for bix, st, ed in batches:
                _one_batch(bix, st, ed)
        else:
            with ThreadPoolExecutor(max_workers=max_rw) as ex:
                futs = [ex.submit(_one_batch, bix, st, ed) for bix, st, ed in batches]
                for f in as_completed(futs):
                    f.result()

    _merge_lc_into_refined_rows(rows, by_tihao)
    jout.parent.mkdir(parents=True, exist_ok=True)
    j_full: list[dict[str, Any]] = []
    for row in rows:
        th, _, _ = qa_from_export_row(row)
        j_full.append(
            {
                "题号": th,
                "要点": str(row.get("要点") or ""),
                "讲课内容": str(row.get("讲课内容") or ""),
            }
        )
    jout.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in j_full) + ("\n" if j_full else ""),
        encoding="utf-8",
    )
    write_refined_rows_to_xlsx(xout, rows)
    write_lecture_handout_docx(dout, rows)
    return {
        "n_pending": total,
        "n_stream_rows": len(flat_log),
        "n_batches": n_batches,
        "out_xlsx": str(xout),
        "out_csv": str(cout),
        "out_docx": str(dout),
        "out_jsonl": str(jout),
        "model": text_model,
        "web_search": web_search,
    }


def run_lecture_content_from_xlsx(
    *,
    in_xlsx: Path,
    out_xlsx: Path | None = None,
    out_csv: Path | None = None,
    out_docx: Path | None = None,
    out_jsonl: Path | None = None,
    config_path: Path | None = None,
    model: str | None = None,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """从结果 xlsx 批注「要点」「讲课内容」，另存 xlsx + 讲义 docx（流式 CSV 按行号续跑）。"""
    cfg = load_config(config_path)
    tl = llm_text_settings()
    if not tl.get("api_key"):
        raise ValueError("未配置 LLM_API_KEY：请在 .env 中设置 LLM_BASE_URL 与 LLM_API_KEY")

    lcfg = dict(cfg.get("lecture_content") or {})
    rcfg = dict(cfg.get("refine") or {})
    text_model = (model or "").strip() or str(
        (lcfg.get("model") or rcfg.get("model") or tl.get("text_model") or "gpt-4o-mini")
    )
    web_search = bool(lcfg.get("web_search", False))
    temp = float(lcfg.get("temperature", 0.4))
    timeout = float(
        lcfg.get("timeout_seconds", rcfg.get("timeout_seconds", 300.0) or 300.0) or 300.0
    )
    batch_size = int(lcfg.get("batch_size", 1) or 1)
    if batch_size < 1:
        batch_size = 1

    default_headers: dict[str, str] | None = None
    burl = (tl.get("base_url") or "") or ""
    if web_search and "dashscope" in burl.lower():
        default_headers = {"X-DashScope-Enable-Internet-Search": "enable"}

    src = in_xlsx.resolve()
    if not src.is_file():
        raise FileNotFoundError(f"未找到 xlsx: {src}")

    base = src.stem
    _dir = src.parent
    xout = (out_xlsx or (_dir / f"{base}_lecture_content.xlsx")).resolve()
    cout = (out_csv or (_dir / f"{base}_lecture_content_stream.csv")).resolve()
    dout = (out_docx or (xout.parent / f"{xout.stem}_讲课稿.docx")).resolve()
    jout = (out_jsonl or (_dir / f"{base}_lecture_content.jsonl")).resolve()

    done_rows = load_done_rownums_from_lecture_content_csv(cout)
    by_row = _load_lc_map_from_row_csv(cout)

    headers, data = read_result_xlsx(src)
    if not data:
        raise ValueError("xlsx 中无数据行（或表为空）")

    work: list[tuple[int, dict[str, str]]] = []
    for excel_row, d in data:
        if int(excel_row) in done_rows:
            continue
        t, q, a = qa_from_export_row(d)
        if not (t or q or a):
            continue
        work.append((excel_row, d))

    lsys = lecture_content_system()
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
        csv_lock = Lock()
        prog_lock = Lock()
        n_done = 0

        def _one_batch(bix: int, st: int, ed: int) -> None:
            nonlocal n_done
            chunk = work[st:ed]
            items: list[dict[str, str | int]] = []
            for excel_row, d in chunk:
                t, q, a = qa_from_export_row(d)
                items.append({"行号": int(excel_row), "题号": t, "问题": q, "解析": a})
            c = OpenAICompatClient(
                api_key=str(tl.get("api_key")),
                base_url=tl.get("base_url") or None,
                default_headers=default_headers,
            )
            up = lecture_content_user_payload_by_row(
                batch_id=f"{Path(src).name} #{bix}/{n_batches}",
                items=items,
                web_search=web_search,
            )

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
                    "lecture-content(xlsx) 重试 batch=%s/%s att=%s/3 sleep=%.2fs err=%s: %s",
                    bix,
                    n_batches,
                    att + 1,
                    s,
                    type(e).__name__,
                    e,
                ),
            )
            parsed = _parse_items_json(content)
            by_out: dict[int, tuple[str, str]] = {}
            for it in parsed:
                rk = it.get("行号")
                try:
                    rnum = int(rk)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
                by_out[rnum] = (
                    str(it.get("要点", "") or "").strip(),
                    str(it.get("讲课内容", "") or "").strip(),
                )

            flat_csv: list[dict[str, Any]] = []
            for excel_row, d in chunk:
                t, _, _ = qa_from_export_row(d)
                pt, lc = by_out.get(int(excel_row), ("", ""))
                flat_csv.append(
                    {
                        "行号": int(excel_row),
                        "题号": t,
                        "要点": pt,
                        "讲课内容": lc,
                    }
                )
            try:
                with csv_lock:
                    append_lecture_content_row_csv(cout, flat_csv)
            except Exception as e:  # noqa: BLE001
                _log.warning("写讲课内容 CSV 失败: %s", e)
            else:
                for rec in flat_csv:
                    rn = int(rec["行号"])
                    ptk = str(rec.get("要点", "") or "").strip()
                    ltk = str(rec.get("讲课内容", "") or "").strip()
                    by_row[rn] = (
                        str(rec.get("题号", "") or ""),
                        ptk,
                        ltk,
                    )
                    if ptk and ltk:
                        done_rows.add(rn)
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

        if max_rw <= 1 or n_batches <= 1:
            for bix, st, ed in batches:
                _one_batch(bix, st, ed)
        else:
            with ThreadPoolExecutor(max_workers=max_rw) as ex:
                futs = [ex.submit(_one_batch, bix, st, ed) for bix, st, ed in batches]
                for f in as_completed(futs):
                    f.result()

    h_final, x_rows = _merge_lc_xlsx_rows(headers, data, by_row)
    jout.parent.mkdir(parents=True, exist_ok=True)
    j_full: list[dict[str, Any]] = []
    for excel_row, d in data:
        t, _, _ = qa_from_export_row(d)
        _, pt, lc = by_row.get(int(excel_row), ("", "", ""))
        if int(excel_row) not in by_row:
            pt = str(d.get("要点") or "")
            lc = str(d.get("讲课内容") or "")
        j_full.append({"行号": excel_row, "题号": t, "要点": pt, "讲课内容": lc})
    jout.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in j_full) + ("\n" if j_full else ""),
        encoding="utf-8",
    )
    write_lecture_tips_xlsx(xout, h_final, x_rows)
    handout_rows: list[dict[str, Any]] = []
    for excel_row, d in data:
        ir = int(excel_row)
        if ir in by_row:
            _, pt, lc = by_row[ir]
        else:
            pt = str(d.get("要点") or "")
            lc = str(d.get("讲课内容") or "")
        handout_rows.append(
            {
                "题号": qa_from_export_row(d)[0],
                "修正后问题": d.get("修正后问题") or "",
                "原问题": d.get("原问题") or "",
                "修正后解析": d.get("修正后解析") or "",
                "原解析": d.get("原解析") or "",
                "要点": pt,
                "讲课内容": lc,
            }
        )
    write_lecture_handout_docx(dout, handout_rows)

    return {
        "n_pending_rows": total,
        "n_stream_rows": len(flat_log),
        "n_batches": n_batches,
        "out_xlsx": str(xout),
        "out_csv": str(cout),
        "out_docx": str(dout),
        "out_jsonl": str(jout),
        "model": text_model,
        "web_search": web_search,
        "in_xlsx": str(src),
    }
