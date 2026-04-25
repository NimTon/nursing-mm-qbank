from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, cast

from mm_qbank.config import load_config, llm_text_settings, project_root
from mm_qbank.io.xlsx_out import write_refined_rows_to_xlsx
from mm_qbank.llm.client import OpenAICompatClient
from mm_qbank.llm.jsonutil import parse_json_object
from mm_qbank.pipeline.llm_compose_run import _manifest_out_root
from mm_qbank.pipeline.vlm_merge import merge_vlm_items_by_tihao
from mm_qbank.prompts_loader import refine_system, refine_user_payload

_log = logging.getLogger(__name__)


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


def _text_norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _normalize_out_item(
    m: dict[str, str], raw: dict[str, Any]
) -> dict[str, Any]:
    """以合并表 `m` 为准定「原*」，以模型出「修正*」等；`修正状态` 由后处理按文本是否实质变化重算。"""
    题 = m.get("题号", "")
    原问 = m.get("问题", "")
    原析 = m.get("解析", "")
    修q = str(raw.get("修正后问题", "") or "").strip() or 原问
    修a = str(raw.get("修正后解析", "") or "").strip() or 原析
    return {
        "题号": str(raw.get("题号", 题) or 题).strip() or 题,
        "原问题": 原问,
        "原解析": 原析,
        "修正后问题": 修q,
        "修正问题原因": str(raw.get("修正问题原因", "") or "").strip(),
        "修正问题参考来源": str(raw.get("修正问题参考来源", "") or "").strip(),
        "修正后解析": 修a,
        "修正解析原因": str(raw.get("修正解析原因", "") or "").strip(),
        "修正解析参考来源": str(raw.get("修正解析参考来源", "") or "").strip(),
        "修正状态": _text_norm(修q) != _text_norm(原问) or _text_norm(修a) != _text_norm(原析),
    }


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
    out_xlsx: Path | None = None,
    config_path: Path | None = None,
    model: str | None = None,
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
    xout = (out_xlsx or (out_dir / "refined_merged.xlsx")).resolve()

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
        n_batches = (total + batch_size - 1) // batch_size
        max_rw = _cap_workers(n_batches, rcfg.get("max_workers"))
        _log.info("vlm-refine 题块批次数=%s，并行：max_workers=%s", n_batches, max_rw)

        def _refine_one_start(bi: int) -> tuple[int, list[dict[str, Any]]]:
            chunk = merged_all[bi : bi + batch_size]
            chunk_simple = [{"题号": x["题号"], "问题": x["问题"], "解析": x["解析"]} for x in chunk]
            up = refine_user_payload(
                page_id=f"跨页合并 {bi//batch_size + 1}", merged=chunk_simple, web_search=web_search
            )
            c = OpenAICompatClient(
                api_key=str(tl.get("api_key")),
                base_url=tl.get("base_url") or None,
                default_headers=default_headers,
            )
            _log.info(
                "[%s/%s] 跨页合并题块 %s 条，请求修正模型",
                (bi // batch_size) + 1,
                n_batches,
                len(chunk_simple),
            )
            try:
                content = c.chat_text_json(
                    model=text_model,
                    messages=[
                        {"role": "system", "content": rsys},
                        {"role": "user", "content": up},
                    ],
                    temperature=temp,
                    timeout=timeout,
                )
            except Exception:  # noqa: BLE001
                content = c.chat_text_json(
                    model=text_model,
                    messages=[
                        {"role": "system", "content": rsys},
                        {"role": "user", "content": up},
                    ],
                    temperature=temp,
                    timeout=None,
                )
            out_items = _parse_refine_response(content)
            flat_part: list[dict[str, Any]] = []
            for j, m in enumerate(chunk_simple):
                oi: dict[str, Any] = out_items[j] if j < len(out_items) else {}
                row = _normalize_out_item(m, oi)
                meta = chunk[j]
                row["page_id"] = ",".join(meta.get("page_ids") or [])
                imgs = meta.get("source_images") or []
                row["source_image"] = imgs[0] if imgs else ""
                flat_part.append(row)
            return (bi, flat_part)

        starts = list(range(0, total, batch_size))
        if max_rw <= 1 or len(starts) <= 1:
            for st in starts:
                _, part = _refine_one_start(st)
                flat.extend(part)
        else:
            with ThreadPoolExecutor(max_workers=max_rw) as ex:
                futs = [ex.submit(_refine_one_start, st) for st in starts]
                parts: list[tuple[int, list[dict[str, Any]]]] = []
                for fut in as_completed(futs):
                    parts.append(fut.result())
            for st, part in sorted(parts, key=lambda t: t[0]):
                flat.extend(part)

    if not flat:
        _log.warning("无输出记录")
    jout.parent.mkdir(parents=True, exist_ok=True)
    jout.write_text(
        "\n".join(json.dumps(x, ensure_ascii=False) for x in flat) + ("\n" if flat else ""),
        encoding="utf-8",
    )
    write_refined_rows_to_xlsx(xout, flat)
    return {
        "n_rows": len(flat),
        "out_jsonl": str(jout),
        "out_xlsx": str(xout),
        "model": text_model,
        "web_search": web_search,
    }
