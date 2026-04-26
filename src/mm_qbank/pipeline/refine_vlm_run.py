from __future__ import annotations

import json
import logging
import os
import re
import csv
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, cast
import math

from mm_qbank.config import load_config, llm_text_settings, project_root
from mm_qbank.io.csv_out import append_refined_rows_to_csv
from mm_qbank.io.xlsx_out import write_refined_rows_to_xlsx
from mm_qbank.llm.client import OpenAICompatClient
from mm_qbank.llm.jsonutil import parse_json_object
from mm_qbank.llm.retry import call_with_retries_timeout
from mm_qbank.pipeline.llm_compose_run import _manifest_out_root
from mm_qbank.pipeline.vlm_merge import merge_vlm_items_by_tihao
from mm_qbank.pipeline.vlm_text_run import run_vlm_text_only
from mm_qbank.prompts_loader import refine_system, refine_user_payload

_log = logging.getLogger(__name__)

_TIHAO_NUM = re.compile(r"\d+")


def _tihao_sort_key(x: dict[str, Any]) -> tuple[int, int, str]:
    """
    题号排序键（升序）：
    - 优先提取第一个数字作为主排序
    - 无数字的放到最后
    - 同数字时按原字符串再排一次，保证稳定可读
    """
    s = str(x.get("题号", "") or "").strip()
    m = _TIHAO_NUM.search(s)
    if not m:
        return (1, 10**12, s)
    try:
        n = int(m.group(0))
    except ValueError:
        return (1, 10**12, s)
    return (0, n, s)


def _tihao_base(s: str) -> str:
    """把 12-1 / 12a / 12（1）等映射回基础题号 '12'；取不到则回退原字符串。"""
    t = (s or "").strip()
    m = _TIHAO_NUM.search(t)
    return (m.group(0) if m else t)


def _normalize_out_item_flexible(
    merged_map: dict[str, dict[str, str]], raw: dict[str, Any]
) -> dict[str, Any]:
    """
    允许 LLM 拆分题号（如 12-1/12-2）：
    - 若 raw 提供了 原问题/原解析，则以 raw 为准（用于“多题混入”拆分后分别落行）
    - 否则用 merged_map[基础题号] 回填
    - `修正状态` 仍按“修正后问题/解析 vs 原问题/解析”判断
    """
    tihao = str(raw.get("题号", "") or "").strip()
    base = _tihao_base(tihao)
    m = merged_map.get(base, {"题号": base, "问题": "", "解析": ""})
    原问 = str(raw.get("原问题") or "").strip() or str(m.get("问题") or "").strip()
    原析 = str(raw.get("原解析") or "").strip() or str(m.get("解析") or "").strip()

    修q_raw = str(raw.get("修正后问题", "") or "").strip()
    修a_raw = str(raw.get("修正后解析", "") or "").strip()
    修q = 修q_raw or 原问
    修a = 修a_raw or 原析
    changed = _text_norm(修q) != _text_norm(原问) or _text_norm(修a) != _text_norm(原析)

    return {
        "题号": tihao or str(raw.get("题号", base) or base).strip() or base,
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


def _cap_workers(n_tasks: int, raw: Any) -> int:
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        w = min(8, (os.cpu_count() or 4) * 2)
    else:
        w = int(raw)
    w = max(1, w)
    return min(w, max(1, n_tasks))


def _read_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _read_manifest(mpath: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in mpath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(json.loads(line))
    return out


def _load_done_tihao_from_csv(path: Path) -> set[str]:
    """读取已有流式 CSV，返回已处理的题号集合（用于断点续跑跳过）。"""
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
                n = str(row.get("题号", "") or "").strip()
                if n:
                    done.add(n)
    except Exception as e:  # noqa: BLE001
        _log.warning("读取已存在 CSV 失败（将不跳过已处理题）：%s (%s)", p, e)
        return set()
    return done


def _text_norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _normalize_out_item(
    m: dict[str, str], raw: dict[str, Any]
) -> dict[str, Any]:
    """以合并表 `m` 为准定「原*」，对模型输出做归一化。

    规则：当 `修正状态=false`（或输出与原文等价）时，除 `题号` / `原问题` / `原解析` 外其余字段必须清空。
    """
    题 = m.get("题号", "")
    原问 = m.get("问题", "")
    原析 = m.get("解析", "")
    修q_raw = str(raw.get("修正后问题", "") or "").strip()
    修a_raw = str(raw.get("修正后解析", "") or "").strip()
    修q = 修q_raw or 原问
    修a = 修a_raw or 原析
    changed = _text_norm(修q) != _text_norm(原问) or _text_norm(修a) != _text_norm(原析)

    out = {
        "题号": str(raw.get("题号", 题) or 题).strip() or 题,
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
    return out


def _parse_refine_response(raw: str) -> list[dict[str, Any]]:
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


def run_refine_vlm_merged(
    *,
    manifest_path: Path,
    out_jsonl: Path | None = None,
    out_csv: Path | None = None,
    out_xlsx: Path | None = None,
    config_path: Path | None = None,
    model: str | None = None,
    on_refine_progress: Any | None = None,
) -> dict[str, Any]:
    """
    读 ``vlm-text`` 的 ``pages.jsonl``，对每页 ``structured_file`` 按题号合并问题/解析，再经文本模型
    输出修正字段，汇总为行列表并写 ``out_jsonl`` 与 **xlsx**（每行一题一记录）。
    """
    cfg = load_config(config_path)
    tl = llm_text_settings()
    if not tl.get("api_key"):
        raise ValueError("未配置 LLM_API_KEY：请在 .env 中设置 LLM_BASE_URL 与 LLM_API_KEY")
    rcfg = dict(cfg.get("refine") or {})
    text_model = (model or "").strip() or str(
        (rcfg.get("model") or tl.get("text_model") or "gpt-4o-mini")
    )
    web_search = bool(rcfg.get("web_search", False))
    temp = float(rcfg.get("temperature", 0.2))
    timeout = float(rcfg.get("timeout_seconds", 180.0))

    mpath = manifest_path.resolve()
    if not mpath.is_file():
        raise FileNotFoundError(f"未找到 manifest: {mpath}")
    work = _manifest_out_root(mpath)
    rows = _read_manifest(mpath)
    if not rows:
        raise ValueError("manifest 为空")

    default_headers: dict[str, str] | None = None
    burl = (tl.get("base_url") or "") or ""
    if web_search and "dashscope" in burl.lower():
        # 部分 OpenAI 兼容网关在部分线路上支持该头触发联网，是否生效以供应商文档为准
        default_headers = {"X-DashScope-Enable-Internet-Search": "enable"}

    root = project_root()
    out_dir = (out_jsonl.parent if out_jsonl else (root / "data" / "out" / "refine")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    jout = (out_jsonl or (out_dir / "refined_merged.jsonl")).resolve()
    cout = (out_csv or (out_dir / "refined_merged_stream.csv")).resolve()
    xout = (out_xlsx or (out_dir / "refined_merged.xlsx")).resolve()

    done_tihao = _load_done_tihao_from_csv(cout)
    if done_tihao:
        _log.info("检测到已存在 CSV，将跳过已处理题号数=%s -> %s", len(done_tihao), str(cout))

    batch_size = int(rcfg.get("batch_size", 20) or 20)
    if batch_size < 1:
        batch_size = 20

    flat: list[dict[str, Any]] = []
    # 关键：VLM 完整跑完后，跨页按题号聚合成「题目块」，后续不再按页；再按题目数分批请求 LLM 修正与导出 xlsx。
    agg: dict[str, dict[str, Any]] = {}
    for i, mrow in enumerate(rows, start=1):
        page_id = str(mrow.get("page_id", ""))
        src = str(mrow.get("source_image", "") or "")
        rel = str(mrow.get("structured_file", "")).replace("\\", "/")
        if not rel.strip() or ".." in rel or rel.startswith("/"):
            _log.warning("[%s/%s] 跳过 page_id=%s 无有效 structured_file", i, len(rows), page_id)
            continue
        p = (work / rel).resolve()
        if not p.is_file():
            _log.warning("[%s/%s] 未找到: %s", i, len(rows), p)
            continue
        try:
            vlm_doc = _read_json(p)
        except Exception as e:  # noqa: BLE001
            _log.warning("读取 JSON 失败 %s: %s", p, e)
            continue
        items = vlm_doc.get("items")
        if not isinstance(items, list):
            continue
        merged_page = merge_vlm_items_by_tihao(cast(list, items))
        if not merged_page:
            continue
        for m in merged_page:
            n = str(m.get("题号", "") or "").strip()
            if not n:
                continue
            a = agg.get(n)
            if a is None:
                agg[n] = {
                    "题号": n,
                    "问题_parts": [],
                    "解析_parts": [],
                    "page_ids": [],
                    "source_images": [],
                }
                a = agg[n]
            q = str(m.get("问题", "") or "").strip()
            r = str(m.get("解析", "") or "").strip()
            if q:
                a["问题_parts"].append(q)
            if r:
                a["解析_parts"].append(r)
            if page_id and page_id not in a["page_ids"]:
                a["page_ids"].append(page_id)
            if src and src not in a["source_images"]:
                a["source_images"].append(src)

    merged_all: list[dict[str, Any]] = []
    for n in sorted(agg.keys(), key=lambda k: re.sub(r"\D", "", k) or k):
        if n in done_tihao:
            continue
        a = agg[n]
        merged_all.append(
            {
                "题号": n,
                "问题": "\n\n".join(a["问题_parts"]).strip(),
                "解析": "\n\n".join(a["解析_parts"]).strip(),
                "page_ids": a["page_ids"],
                "source_images": a["source_images"],
            }
        )

    if not merged_all:
        _log.warning("跨页聚合后无可修正条目")
    else:
        rsys = refine_system()
        total = len(merged_all)
        merged_map: dict[str, dict[str, str]] = {
            str(x.get("题号") or "").strip(): {"题号": str(x.get("题号") or "").strip(), "问题": str(x.get("问题") or ""), "解析": str(x.get("解析") or "")}
            for x in merged_all
            if str(x.get("题号") or "").strip()
        }

        raw_max_workers = rcfg.get("max_workers")

        # 分批策略：
        # - total > batch_size：按 batch_size 切批；并发由 max_workers 限制
        # - total <= batch_size：避免只有 1 批导致“看起来不并行”，改为按 max_workers 尽量均分切成多批并行
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
            "vlm-refine 题块总数=%s，批次数=%s（chunk_size=%s, batch_size=%s），并行：max_workers=%s",
            total,
            n_batches,
            chunk_size,
            batch_size,
            max_rw,
        )

        csv_lock = Lock()
        prog_lock = Lock()
        n_done_total = 0

        def _refine_one_batch(batch_idx: int, st: int, ed: int) -> tuple[int, list[dict[str, Any]]]:
            chunk = merged_all[st:ed]
            chunk_simple: list[dict[str, Any]] = [
                {"题号": x["题号"], "问题": x["问题"], "解析": x["解析"]} for x in chunk
            ]
            up = refine_user_payload(
                page_id=f"跨页合并 {batch_idx}", merged=chunk_simple, web_search=web_search
            )
            c = OpenAICompatClient(
                api_key=str(tl.get("api_key")),
                base_url=tl.get("base_url") or None,
                default_headers=default_headers,
            )
            _log.info(
                "[%s/%s] 跨页合并题块 %s 条，请求修正模型",
                batch_idx,
                n_batches,
                len(chunk_simple),
            )
            def _call_once(tout: float) -> str:
                try:
                    return c.chat_text_json(
                        model=text_model,
                        messages=[
                            {"role": "system", "content": rsys},
                            {"role": "user", "content": up},
                        ],
                        temperature=temp,
                        timeout=float(tout),
                    )
                except Exception:  # noqa: BLE001
                    # 某些网关对 timeout 参数兼容不好；退化为不传 timeout 的调用
                    return c.chat_text_json(
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
                on_retry=lambda att, e, s, nt: _log.warning(
                    "LLM HTTP 失败将重试 batch=%s/%s attempt=%s/3 sleep=%.2fs next_timeout=%.1fs err=%s: %s",
                    batch_idx,
                    n_batches,
                    att + 1,
                    s,
                    nt,
                    type(e).__name__,
                    e,
                ),
            )
            out_items = _parse_refine_response(content)
            flat_part: list[dict[str, Any]] = []
            # 允许 LLM 拆分题号（同一输入题块输出多行）
            for oi in out_items:
                tihao = str(oi.get("题号", "") or "").strip()
                base = _tihao_base(tihao)
                meta = agg.get(base)
                if meta is None:
                    continue
                m = merged_map.get(base, {"题号": base, "问题": "", "解析": ""})
                row = _normalize_out_item_flexible({base: m}, oi)
                row["page_id"] = ",".join(meta.get("page_ids") or [])
                imgs = meta.get("source_images") or []
                row["source_image"] = imgs[0] if imgs else ""
                flat_part.append(row)

                # 关键：LLM 每完成一题，就立刻追加写入 CSV，并记录日志（并发下加锁保证文件一致性）
                try:
                    with csv_lock:
                        append_refined_rows_to_csv(cout, [row])
                    _log.info(
                        "refine-done tihao=%s changed=%s -> csv=%s",
                        row.get("题号", ""),
                        row.get("修正状态", False),
                        str(cout),
                    )
                except Exception as e:  # noqa: BLE001
                    _log.warning("追加写 CSV 失败 tihao=%s: %s", row.get("题号", ""), e)
                # 进度回调：每处理一题更新一次（须在 try/except 之外，否则 CSV 成功时从不回调）
                if on_refine_progress is not None:
                    try:
                        with prog_lock:
                            n_done_total += 1
                            nd = n_done_total
                        on_refine_progress(int(nd), int(max(total, nd)))
                    except Exception:  # noqa: BLE001
                        pass
            return (st, flat_part)

        if max_rw <= 1 or n_batches <= 1:
            for batch_idx, st, ed in batches:
                _, part = _refine_one_batch(batch_idx, st, ed)
                flat.extend(part)
        else:
            with ThreadPoolExecutor(max_workers=max_rw) as ex:
                futs = [ex.submit(_refine_one_batch, batch_idx, st, ed) for batch_idx, st, ed in batches]
                parts: list[tuple[int, list[dict[str, Any]]]] = []
                for fut in as_completed(futs):
                    parts.append(fut.result())
            for st, part in sorted(parts, key=lambda t: t[0]):
                flat.extend(part)

    if not flat:
        _log.warning("无输出记录")
    # 最终导出统一按题号升序（CSV 仍保持流式追加顺序用于断点续跑）
    flat.sort(key=_tihao_sort_key)
    jout.parent.mkdir(parents=True, exist_ok=True)
    jout.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in flat) + ("\n" if flat else ""),
        encoding="utf-8",
    )
    write_refined_rows_to_xlsx(xout, flat)
    return {
        "n_rows": len(flat),
        "out_jsonl": str(jout),
        "out_csv": str(cout),
        "out_xlsx": str(xout),
        "model": text_model,
        "web_search": web_search,
    }


def run_vlm_text_and_refine_streaming(
    *,
    input_dir: Path,
    out_dir: Path | None = None,
    config_path: Path | None = None,
    vlm_model: str | None = None,
    refine_model: str | None = None,
    out_jsonl: Path | None = None,
    out_csv: Path | None = None,
    out_xlsx: Path | None = None,
    cancel_event: Any | None = None,
    paused: Any | None = None,
    on_vlm_page_done: Any | None = None,
    on_refine_item_done: Any | None = None,
    on_refine_progress: Any | None = None,
) -> dict[str, Any]:
    """
    流水线：VLM 每页完成就合并题号缓冲；当同题「问题+解析」凑齐后立刻进入 LLM 缓冲批；
    满一批就请求 LLM 修正，并流式写 CSV；最终汇总写 jsonl/xlsx。

    目标：不等 VLM 全部跑完就开始 LLM 修正，节省总耗时，并提高中断可恢复性。
    """
    cfg = load_config(config_path)
    tl = llm_text_settings()
    if not tl.get("api_key"):
        raise ValueError("未配置 LLM_API_KEY：请在 .env 中设置 LLM_BASE_URL 与 LLM_API_KEY")
    rcfg = dict(cfg.get("refine") or {})
    text_model = (refine_model or "").strip() or str(
        (rcfg.get("model") or tl.get("text_model") or "gpt-4o-mini")
    )
    web_search = bool(rcfg.get("web_search", False))
    temp = float(rcfg.get("temperature", 0.2))
    timeout = float(rcfg.get("timeout_seconds", 180.0))
    batch_size = int(rcfg.get("batch_size", 20) or 20)
    if batch_size < 1:
        batch_size = 20

    default_headers: dict[str, str] | None = None
    burl = (tl.get("base_url") or "") or ""
    if web_search and "dashscope" in burl.lower():
        default_headers = {"X-DashScope-Enable-Internet-Search": "enable"}

    root = project_root()
    out = (out_dir or (root / "data" / "out" / "vlm_text")).resolve()
    out.mkdir(parents=True, exist_ok=True)

    out_dir2 = (out_jsonl.parent if out_jsonl else (out / "refine")).resolve()
    out_dir2.mkdir(parents=True, exist_ok=True)
    jout = (out_jsonl or (out_dir2 / "refined_merged.jsonl")).resolve()
    cout = (out_csv or (out_dir2 / "refined_merged_stream.csv")).resolve()
    xout = (out_xlsx or (out_dir2 / "refined_merged.xlsx")).resolve()

    done_tihao = _load_done_tihao_from_csv(cout)
    if done_tihao:
        _log.info("检测到已存在 CSV，将跳过已处理题号数=%s -> %s", len(done_tihao), str(cout))

    # VLM → 聚合器 事件队列
    q: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=256)
    vlm_done = Event()
    csv_lock = Lock()

    def _on_page_result(
        idx: int,
        total: int,
        page_id: str,
        source_image: Path,
        items: list[dict[str, Any]],
        extra: dict[str, Any] | None,
        row: dict[str, Any],
    ) -> None:
        # 只关心 items（题号+问题/解析）
        q.put(
            {
                "idx": idx,
                "total": total,
                "page_id": page_id,
                "source_image": str(source_image.resolve()),
                "items": items,
            }
        )

    flat: list[dict[str, Any]] = []
    _n_refined = 0

    def _refine_batch(batch_idx: int, chunk_simple: list[dict[str, str]]) -> list[dict[str, Any]]:
        rsys = refine_system()
        up = refine_user_payload(page_id=f"stream {batch_idx}", merged=chunk_simple, web_search=web_search)
        c = OpenAICompatClient(
            api_key=str(tl.get("api_key")),
            base_url=tl.get("base_url") or None,
            default_headers=default_headers,
        )

        def _call_once(tout: float) -> str:
            try:
                return c.chat_text_json(
                    model=text_model,
                    messages=[{"role": "system", "content": rsys}, {"role": "user", "content": up}],
                    temperature=temp,
                    timeout=float(tout),
                )
            except Exception:  # noqa: BLE001
                return c.chat_text_json(
                    model=text_model,
                    messages=[{"role": "system", "content": rsys}, {"role": "user", "content": up}],
                    temperature=temp,
                    timeout=None,
                )

        content = call_with_retries_timeout(
            _call_once,
            tries=3,
            base_timeout_s=float(timeout),
            base_sleep_s=1.0,
            on_retry=lambda att, e, s, nt: _log.warning(
                "LLM HTTP 失败将重试 stream_batch=%s attempt=%s/3 sleep=%.2fs next_timeout=%.1fs err=%s: %s",
                batch_idx,
                att + 1,
                s,
                nt,
                type(e).__name__,
                e,
            ),
        )
        out_items = _parse_refine_response(content)
        out_rows: list[dict[str, Any]] = []
        for j, m in enumerate(chunk_simple):
            oi: dict[str, Any] = out_items[j] if j < len(out_items) else {}
            out_rows.append(_normalize_out_item(m, oi))
        return out_rows

    def _aggregator() -> None:
        nonlocal _n_refined
        # 题号缓冲：直到同时出现问题+解析才算“凑齐”
        buf: dict[str, dict[str, Any]] = {}
        ready: list[dict[str, Any]] = []
        sent: set[str] = set()
        batch_idx = 0

        def _flush_ready(force: bool = False) -> None:
            nonlocal batch_idx, _n_refined
            while ready and (force or len(ready) >= batch_size):
                batch_idx += 1
                chunk = ready[:batch_size]
                del ready[: min(batch_size, len(chunk))]
                chunk_simple = [{"题号": x["题号"], "问题": x["问题"], "解析": x["解析"]} for x in chunk]
                _log.info("stream-refine 请求 batch=%s 条=%s", batch_idx, len(chunk_simple))
                refined = _refine_batch(batch_idx, chunk_simple)
                for rrow, meta in zip(refined, chunk, strict=False):
                    rrow["page_id"] = ",".join(meta.get("page_ids") or [])
                    imgs = meta.get("source_images") or []
                    rrow["source_image"] = imgs[0] if imgs else ""
                    flat.append(rrow)
                    try:
                        with csv_lock:
                            append_refined_rows_to_csv(cout, [rrow])
                        _log.info(
                            "refine-done tihao=%s changed=%s -> csv=%s",
                            rrow.get("题号", ""),
                            rrow.get("修正状态", False),
                            str(cout),
                        )
                        _n_refined += 1
                        if on_refine_item_done is not None:
                            try:
                                on_refine_item_done(_n_refined)
                            except Exception:  # noqa: BLE001
                                pass
                        if on_refine_progress is not None:
                            try:
                                # 流式模式下总题数无法预先确定，这里用“已发现的题数”做动态估计
                                total_est = len(sent) + len(ready) + len(buf)
                                if total_est < _n_refined:
                                    total_est = _n_refined
                                on_refine_progress(int(_n_refined), int(total_est))
                            except Exception:  # noqa: BLE001
                                pass
                    except Exception as e:  # noqa: BLE001
                        _log.warning("追加写 CSV 失败 tihao=%s: %s", rrow.get("题号", ""), e)

        while True:
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                _log.info("stream-refine 检测到取消，结束聚合器")
                break
            try:
                ev = q.get(timeout=0.25)
            except queue.Empty:
                if vlm_done.is_set():
                    break
                continue

            items = ev.get("items") or []
            page_id = str(ev.get("page_id", "") or "")
            src = str(ev.get("source_image", "") or "")
            merged_page = merge_vlm_items_by_tihao(cast(list, items))
            for m in merged_page:
                n = str(m.get("题号", "") or "").strip()
                if not n or n in done_tihao or n in sent:
                    continue
                a = buf.get(n)
                if a is None:
                    buf[n] = {"题号": n, "问题_parts": [], "解析_parts": [], "page_ids": [], "source_images": []}
                    a = buf[n]
                qtxt = str(m.get("问题", "") or "").strip()
                atxt = str(m.get("解析", "") or "").strip()
                if qtxt:
                    a["问题_parts"].append(qtxt)
                if atxt:
                    a["解析_parts"].append(atxt)
                if page_id and page_id not in a["page_ids"]:
                    a["page_ids"].append(page_id)
                if src and src not in a["source_images"]:
                    a["source_images"].append(src)

                has_q = bool("\n\n".join(a["问题_parts"]).strip())
                has_a = bool("\n\n".join(a["解析_parts"]).strip())
                if has_q and has_a:
                    ready.append(
                        {
                            "题号": n,
                            "问题": "\n\n".join(a["问题_parts"]).strip(),
                            "解析": "\n\n".join(a["解析_parts"]).strip(),
                            "page_ids": a["page_ids"],
                            "source_images": a["source_images"],
                        }
                    )
                    sent.add(n)
                    buf.pop(n, None)
            _flush_ready(force=False)

        _flush_ready(force=True)

    t = Thread(target=_aggregator, daemon=True)
    t.start()

    try:
        vlm_summary = run_vlm_text_only(
            input_dir=input_dir,
            out_dir=out,
            config_path=config_path,
            model=vlm_model,
            cancel_event=cancel_event,
            paused=paused,
            on_page_done=on_vlm_page_done,
            on_page_result=_on_page_result,
        )
    finally:
        vlm_done.set()
        t.join(timeout=3600)

    if not flat:
        _log.warning("stream-refine 无输出记录")
    # 最终导出统一按题号升序（CSV 仍保持流式追加顺序用于断点续跑）
    flat.sort(key=_tihao_sort_key)
    jout.parent.mkdir(parents=True, exist_ok=True)
    jout.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in flat) + ("\n" if flat else ""),
        encoding="utf-8",
    )
    write_refined_rows_to_xlsx(xout, flat)
    return {
        **vlm_summary,
        "mode": "vlm+refine_stream",
        "n_rows": len(flat),
        "out_jsonl": str(jout),
        "out_csv": str(cout),
        "out_xlsx": str(xout),
        "refine_model": text_model,
        "web_search": web_search,
    }


_LEADING_NUM_IN_STEM = re.compile(r"^\s*(\d{1,4})\s*[\.、\)\]】]?\s*")


def _read_compose_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        x = json.loads(line)
        if isinstance(x, dict):
            rows.append(cast(dict[str, Any], x))
    return rows


def run_refine_from_compose_jsonl(
    *,
    compose_jsonl: Path,
    out_jsonl: Path | None = None,
    out_csv: Path | None = None,
    out_xlsx: Path | None = None,
    config_path: Path | None = None,
    model: str | None = None,
    on_refine_progress: Any | None = None,
) -> dict[str, Any]:
    """
    直接读取 llm-compose 的输出（每行一页，含 items=composed_items），将每题 stem/options/analysis
    映射成「问题/解析」后走同一套修正流程，并导出 jsonl/xlsx + 流式 csv。
    """
    cfg = load_config(config_path)
    tl = llm_text_settings()
    if not tl.get("api_key"):
        raise ValueError("未配置 LLM_API_KEY：请在 .env 中设置 LLM_BASE_URL 与 LLM_API_KEY")
    rcfg = dict(cfg.get("refine") or {})
    text_model = (model or "").strip() or str(
        (rcfg.get("model") or tl.get("text_model") or "gpt-4o-mini")
    )
    web_search = bool(rcfg.get("web_search", False))
    temp = float(rcfg.get("temperature", 0.2))
    timeout = float(rcfg.get("timeout_seconds", 180.0))

    default_headers: dict[str, str] | None = None
    burl = (tl.get("base_url") or "") or ""
    if web_search and "dashscope" in burl.lower():
        default_headers = {"X-DashScope-Enable-Internet-Search": "enable"}

    cpath = compose_jsonl.resolve()
    if not cpath.is_file():
        raise FileNotFoundError(f"未找到 compose jsonl: {cpath}")

    root = project_root()
    out_dir = (out_jsonl.parent if out_jsonl else (root / "data" / "out" / "refine")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    jout = (out_jsonl or (out_dir / "refined_from_compose.jsonl")).resolve()
    cout = (out_csv or (out_dir / "refined_from_compose_stream.csv")).resolve()
    xout = (out_xlsx or (out_dir / "refined_from_compose.xlsx")).resolve()

    done_tihao = _load_done_tihao_from_csv(cout)
    if done_tihao:
        _log.info("检测到已存在 CSV，将跳过已处理题号数=%s -> %s", len(done_tihao), str(cout))

    page_rows = _read_compose_jsonl(cpath)
    if not page_rows:
        raise ValueError("compose_jsonl 为空")

    # 聚合成题块（跨页按题号合并）；题号取 stem 开头数字，取不到则按全局序号生成（可断点续跑依赖同一输入）
    agg: dict[str, dict[str, Any]] = {}
    seq = 0
    for prow in page_rows:
        page_id = str(prow.get("page_id", "") or "")
        src = str(prow.get("source_image", "") or "")
        items = prow.get("items")
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            stem = str(it.get("stem", "") or "").strip()
            options = it.get("options") if isinstance(it.get("options"), list) else []
            opt_txt = "\n".join(str(x) for x in options if str(x).strip())
            q = (stem + ("\n" + opt_txt if opt_txt else "")).strip()
            a_txt = str(it.get("analysis", "") or "").strip()
            m = _LEADING_NUM_IN_STEM.match(stem)
            if m:
                n = m.group(1)
            else:
                seq += 1
                n = str(seq)
            if not n or n in done_tihao:
                continue
            a = agg.get(n)
            if a is None:
                agg[n] = {"题号": n, "问题_parts": [], "解析_parts": [], "page_ids": [], "source_images": []}
                a = agg[n]
            if q:
                a["问题_parts"].append(q)
            if a_txt:
                a["解析_parts"].append(a_txt)
            if page_id and page_id not in a["page_ids"]:
                a["page_ids"].append(page_id)
            if src and src not in a["source_images"]:
                a["source_images"].append(src)

    merged_all: list[dict[str, Any]] = []
    for n in sorted(agg.keys(), key=lambda k: re.sub(r"\D", "", k) or k):
        a = agg[n]
        merged_all.append(
            {
                "题号": n,
                "问题": "\n\n".join(a["问题_parts"]).strip(),
                "解析": "\n\n".join(a["解析_parts"]).strip(),
                "page_ids": a["page_ids"],
                "source_images": a["source_images"],
            }
        )

    flat: list[dict[str, Any]] = []
    if not merged_all:
        _log.warning("compose_jsonl 聚合后无可修正条目")
    else:
        rsys = refine_system()
        total = len(merged_all)
        raw_max_workers = rcfg.get("max_workers")
        batch_size = int(rcfg.get("batch_size", 20) or 20)
        if batch_size < 1:
            batch_size = 20
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
            "compose-refine 题块总数=%s，批次数=%s（chunk_size=%s, batch_size=%s），并行：max_workers=%s",
            total,
            n_batches,
            chunk_size,
            batch_size,
            max_rw,
        )
        csv_lock = Lock()
        prog_lock = Lock()
        n_done_total = 0

        def _refine_one_batch(batch_idx: int, st: int, ed: int) -> tuple[int, list[dict[str, Any]]]:
            chunk = merged_all[st:ed]
            chunk_simple: list[dict[str, Any]] = [
                {"题号": x["题号"], "问题": x["问题"], "解析": x["解析"]} for x in chunk
            ]
            up = refine_user_payload(page_id=f"compose {batch_idx}", merged=chunk_simple, web_search=web_search)
            c = OpenAICompatClient(
                api_key=str(tl.get("api_key")),
                base_url=tl.get("base_url") or None,
                default_headers=default_headers,
            )
            _log.info("[%s/%s] compose 题块 %s 条，请求修正模型", batch_idx, n_batches, len(chunk_simple))

            def _call_once(tout: float) -> str:
                try:
                    return c.chat_text_json(
                        model=text_model,
                        messages=[{"role": "system", "content": rsys}, {"role": "user", "content": up}],
                        temperature=temp,
                        timeout=float(tout),
                    )
                except Exception:  # noqa: BLE001
                    return c.chat_text_json(
                        model=text_model,
                        messages=[{"role": "system", "content": rsys}, {"role": "user", "content": up}],
                        temperature=temp,
                        timeout=None,
                    )

            content = call_with_retries_timeout(
                _call_once,
                tries=3,
                base_timeout_s=float(timeout),
                base_sleep_s=1.0,
                on_retry=lambda att, e, s, nt: _log.warning(
                    "LLM HTTP 失败将重试 batch=%s/%s attempt=%s/3 sleep=%.2fs next_timeout=%.1fs err=%s: %s",
                    batch_idx,
                    n_batches,
                    att + 1,
                    s,
                    nt,
                    type(e).__name__,
                    e,
                ),
            )
            out_items = _parse_refine_response(content)
            flat_part: list[dict[str, Any]] = []
            merged_map_batch: dict[str, dict[str, str]] = {
                str(x.get("题号") or "").strip(): {"题号": str(x.get("题号") or "").strip(), "问题": str(x.get("问题") or ""), "解析": str(x.get("解析") or "")}
                for x in chunk
                if str(x.get("题号") or "").strip()
            }
            meta_map_batch: dict[str, dict[str, Any]] = {
                str(x.get("题号") or "").strip(): x for x in chunk if str(x.get("题号") or "").strip()
            }
            for oi in out_items:
                tihao = str(oi.get("题号", "") or "").strip()
                base = _tihao_base(tihao)
                meta = meta_map_batch.get(base)
                if meta is None:
                    continue
                row = _normalize_out_item_flexible(merged_map_batch, oi)
                row["page_id"] = ",".join(meta.get("page_ids") or [])
                imgs = meta.get("source_images") or []
                row["source_image"] = imgs[0] if imgs else ""
                flat_part.append(row)
                try:
                    with csv_lock:
                        append_refined_rows_to_csv(cout, [row])
                    _log.info(
                        "refine-done tihao=%s changed=%s -> csv=%s",
                        row.get("题号", ""),
                        row.get("修正状态", False),
                        str(cout),
                    )
                except Exception as e:  # noqa: BLE001
                    _log.warning("追加写 CSV 失败 tihao=%s: %s", row.get("题号", ""), e)
                if on_refine_progress is not None:
                    try:
                        with prog_lock:
                            n_done_total += 1
                            nd = n_done_total
                        on_refine_progress(int(nd), int(max(total, nd)))
                    except Exception:  # noqa: BLE001
                        pass
            return (st, flat_part)

        if max_rw <= 1 or n_batches <= 1:
            for batch_idx, st, ed in batches:
                _, part = _refine_one_batch(batch_idx, st, ed)
                flat.extend(part)
        else:
            with ThreadPoolExecutor(max_workers=max_rw) as ex:
                futs = [ex.submit(_refine_one_batch, batch_idx, st, ed) for batch_idx, st, ed in batches]
                parts: list[tuple[int, list[dict[str, Any]]]] = []
                for fut in as_completed(futs):
                    parts.append(fut.result())
            for st, part in sorted(parts, key=lambda t: t[0]):
                flat.extend(part)

    if not flat:
        _log.warning("无输出记录")
    # 最终导出统一按题号升序（CSV 仍保持流式追加顺序用于断点续跑）
    flat.sort(key=_tihao_sort_key)
    jout.parent.mkdir(parents=True, exist_ok=True)
    jout.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in flat) + ("\n" if flat else ""),
        encoding="utf-8",
    )
    write_refined_rows_to_xlsx(xout, flat)
    return {
        "n_rows": len(flat),
        "out_jsonl": str(jout),
        "out_csv": str(cout),
        "out_xlsx": str(xout),
        "model": text_model,
        "web_search": web_search,
        "compose_jsonl": str(cpath),
    }
