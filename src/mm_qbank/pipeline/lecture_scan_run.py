from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mm_qbank.config import load_config, llm_text_settings, project_root, resolve_llm_model, resolve_vlm_model, vlm_settings
from mm_qbank.io.xlsx_out import write_refined_rows_to_xlsx
from mm_qbank.llm.client import OpenAICompatClient
from mm_qbank.llm.jsonutil import parse_json_object
from mm_qbank.llm.retry import call_with_retries_timeout
from mm_qbank.pipeline.lecture_content_run import run_lecture_content_on_refined_rows
from mm_qbank.pipeline.refine_run import run_refine_on_rows
from mm_qbank.pipeline.scan_common import cap_workers, content_type, wait_unpaused
from mm_qbank.pipeline.scan_pages import list_input_images, page_id_for
from mm_qbank.preprocess.image import preprocess_image
from mm_qbank.prompts_loader import lecture_scan_assemble_prompts, lecture_scan_vlm_prompts

_log = logging.getLogger(__name__)

_SCAN_SUBDIR = "scan_pages"
_SCAN_MANIFEST = "scan_pages.jsonl"
_PAGE_MARKER = "=== 页码 {page} ==="

# 后处理兜底：去掉常见培训机构噪音行
_NOISE_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"启峻", re.I),
    re.compile(r"让梦想起飞", re.I),
    re.compile(r"祝您考试成功", re.I),
    re.compile(r"报名热线|咨询热线", re.I),
    re.compile(r"1[3578]\d{9}"),  # 常见手机号段
)


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


def parse_page_number_for_sort(页码: str, *, file_order: int) -> tuple[int, int]:
    """返回 (主排序键, 次键)。无页码时用极大主键 + 文件顺序。"""
    s = str(页码 or "").strip()
    nums = re.findall(r"\d+", s)
    if nums:
        return (int(nums[-1]), file_order)
    return (10**9, file_order)


def filter_noise_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        t = str(line or "").strip()
        if not t:
            continue
        if any(p.search(t) for p in _NOISE_LINE_PATTERNS):
            continue
        out.append(t)
    return out


def _parse_scan_vlm_response(
    raw: str,
) -> tuple[str, str, list[str], dict[str, Any] | None]:
    try:
        s = _strip_wrapping_artifacts(raw)
        data = parse_json_object(s)
    except Exception:  # noqa: BLE001
        return "", "", [], {"parse_error": "json", "raw_preview": raw[:8000]}
    if not isinstance(data, dict):
        return "", "", [], {"parse_error": "not_object", "raw_preview": raw[:8000]}
    page_no = str(data.get("页码", data.get("page_number", "")) or "").strip()
    chapter = str(
        data.get("章节标题", data.get("chapter_title", data.get("章节", ""))) or ""
    ).strip()
    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list):
        raw_lines = data.get("识别内容") or data.get("content")
    lines: list[str] = []
    if isinstance(raw_lines, list):
        for x in raw_lines:
            if x is None:
                continue
            t = str(x).replace("\r\n", "\n").strip()
            if t:
                lines.append(t)
    elif isinstance(raw_lines, str) and raw_lines.strip():
        lines = [ln.strip() for ln in raw_lines.replace("\r\n", "\n").split("\n") if ln.strip()]
    lines = filter_noise_lines(lines)
    return page_no, chapter, lines, None


def _read_scan_manifest(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _manifest_out_root(manifest_path: Path) -> Path:
    return manifest_path.parent.parent.resolve()


def sanitize_chapter_title_for_filename(title: str) -> str:
    """章节标题 → 安全文件名（不含扩展名）。"""
    s = str(title or "").strip()
    if not s:
        return ""
    s = re.sub(r'[\\/:*?"<>|]+', "_", s)
    s = re.sub(r"\s+", "_", s)
    s = s.strip("._")
    return s[:80]


def resolve_export_chapter_title(pages: list[dict[str, Any]]) -> str:
    """按阅读顺序取首个非空章节标题（用于讲义 Word 文件名）。"""
    for row in sort_scan_pages(pages):
        t = str(row.get("章节标题") or "").strip()
        if t:
            return t
    return ""


def lecture_scan_handout_docx_path(out_dir: Path, chapter_title: str) -> Path:
    safe = sanitize_chapter_title_for_filename(chapter_title)
    if safe:
        return (out_dir / f"{safe}_讲课稿.docx").resolve()
    return (out_dir / "lecture_scan_讲课稿.docx").resolve()


def sort_scan_pages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for i, row in enumerate(rows, start=1):
        fo = int(row.get("file_order") or i)
        page_no = str(row.get("页码") or row.get("page_number") or "").strip()
        key = parse_page_number_for_sort(page_no, file_order=fo)
        ch = str(row.get("章节标题") or "").strip()
        indexed.append((key, {**row, "页码": page_no, "file_order": fo, "章节标题": ch}))
    indexed.sort(key=lambda x: x[0])
    return [r for _, r in indexed]


def build_concatenated_raw_text(pages: list[dict[str, Any]], *, work: Path) -> str:
    """按已排序页读取 scan json 中的 lines 并拼接。"""
    parts: list[str] = []
    root = work.resolve()
    for row in pages:
        page_no = str(row.get("页码") or "").strip() or "?"
        rel = str(row.get("scan_file") or row.get("structured_file") or "").replace("\\", "/")
        lines: list[str] = []
        if rel and ".." not in rel:
            p = (root / rel).resolve()
            if p.is_file():
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                    raw = doc.get("lines")
                    if isinstance(raw, list):
                        lines = filter_noise_lines([str(x) for x in raw if str(x).strip()])
                except Exception:  # noqa: BLE001
                    pass
        marker = _PAGE_MARKER.format(page=page_no)
        parts.append(marker)
        parts.extend(lines)
    return "\n".join(parts)


def _scan_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return dict(cfg.get("lecture_scan") or {})


def _resolve_scan_vlm_workers(cfg: dict[str, Any], n_tasks: int) -> int:
    lsc = _scan_cfg(cfg)
    raw = (lsc.get("vlm") or {}).get("max_workers")
    if raw is None:
        raw = (cfg.get("vlm") or {}).get("max_workers")
    return cap_workers(n_tasks, raw)


def _resolve_scan_lecture_workers(cfg: dict[str, Any], n_tasks: int) -> int:
    lsc = _scan_cfg(cfg)
    raw = (lsc.get("lecture") or {}).get("max_workers")
    if raw is None:
        raw = (cfg.get("lecture_content") or {}).get("max_workers")
    return cap_workers(n_tasks, raw)


def run_lecture_scan_vlm(
    *,
    input_dir: Path,
    out_dir: Path | None = None,
    config_path: Path | None = None,
    model: str | None = None,
    cancel_event: threading.Event | None = None,
    paused: Callable[[], bool] | None = None,
    on_page_done: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """阶段 1：每张图 VLM 输出页码 + 逐行原文。"""
    cfg = load_config(config_path)
    vs = vlm_settings()
    if not vs.get("api_key"):
        raise ValueError("未配置 VLM_API_KEY")
    lsc = _scan_cfg(cfg)
    vlm_cfg = dict(lsc.get("vlm") or {})
    pre_cfg = dict(cfg.get("preprocess") or {})
    if (model or "").strip():
        m_model = (model or "").strip()
    else:
        m_model = resolve_vlm_model(cfg)
    temp = float(vlm_cfg.get("temperature", cfg.get("vlm", {}).get("temperature", 0.1)))
    timeout = float(vlm_cfg.get("timeout_seconds", cfg.get("vlm", {}).get("timeout_seconds", 300.0)))
    use_pre = bool(vlm_cfg.get("use_preprocess", True))
    max_long_edge = int(pre_cfg.get("max_long_edge", 2000) or 2000)

    root = project_root()
    out = (out_dir or (root / "data" / "out" / "lecture_scan")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    images = list_input_images(input_dir)
    if not images:
        raise FileNotFoundError(f"目录中未找到图片: {input_dir}")

    p_sys, p_user = lecture_scan_vlm_prompts()
    api_key = str(vs.get("api_key"))
    base_url = vs.get("base_url") or None
    n_img = len(images)
    max_workers = _resolve_scan_vlm_workers(cfg, n_img)
    _log.info(
        "lecture-scan 阶段1 VLM：%s 张图，并行 max_workers=%s（预处理与 API 可并行，拆题前完成）",
        n_img,
        max_workers,
    )

    man_path = (out / _SCAN_SUBDIR / _SCAN_MANIFEST).resolve()
    man_path.parent.mkdir(parents=True, exist_ok=True)
    man_f = man_path.open("w", encoding="utf-8", newline="")
    manifest_rows: list[dict[str, Any]] = []
    cancelled = False
    _man_lock = threading.Lock()
    _pending: dict[int, dict[str, Any]] = {}
    _next_idx = 1

    def _stream_row(idx: int, row: dict[str, Any]) -> None:
        nonlocal _next_idx
        with _man_lock:
            _pending[idx] = row
            while _next_idx in _pending:
                r = _pending.pop(_next_idx)
                man_f.write(json.dumps(r, ensure_ascii=False) + "\n")
                man_f.flush()
                manifest_rows.append(r)
                _next_idx += 1

    with tempfile.TemporaryDirectory(prefix="mm_qbank_lscan_") as tmp:
        tdir = Path(tmp)

        def _run_one(i: int, img_path: Path) -> tuple[int, dict[str, Any] | None, bool]:
            if cancel_event is not None and cancel_event.is_set():
                return (i, None, True)
            if wait_unpaused(pause=paused, cancel=cancel_event):
                return (i, None, True)
            page_id = page_id_for(img_path, root=input_dir)
            if use_pre:
                pre_path = tdir / f"{page_id}_{threading.get_ident()}.png"
                preprocess_image(
                    img_path,
                    pre_path,
                    max_long_edge=max_long_edge,
                    deskew=pre_cfg.get("deskew"),
                    tone_mode=str(pre_cfg.get("tone_mode", "raw")),
                    auto_rotate_ocr=pre_cfg.get("auto_rotate_ocr"),
                    perspective=pre_cfg.get("perspective"),
                )
                body = pre_path.read_bytes()
                ctyp = "image/png"
            else:
                body = img_path.read_bytes()
                ctyp = content_type(img_path.suffix)
            if cancel_event is not None and cancel_event.is_set():
                return (i, None, True)
            cl = OpenAICompatClient(api_key=api_key, base_url=base_url)
            raw = call_with_retries_timeout(
                lambda t: cl.chat_vision(
                    model=m_model,
                    system=p_sys,
                    user_text=p_user,
                    image_bytes=body,
                    content_type=ctyp,
                    temperature=temp,
                    timeout=float(t),
                    response_format_json=True,
                ),
                tries=3,
                base_timeout_s=float(timeout),
                base_sleep_s=1.0,
            )
            page_no, chapter, lines, extra = _parse_scan_vlm_response(raw)
            base = out / _SCAN_SUBDIR
            base.mkdir(parents=True, exist_ok=True)
            doc: dict[str, Any] = {
                "page_id": page_id,
                "页码": page_no,
                "章节标题": chapter,
                "lines": lines,
                "source_image": str(img_path.resolve()),
                "file_order": i,
            }
            if extra:
                doc["extra"] = extra
            json_path = (base / f"{page_id}.json").resolve()
            json_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            rel = json_path.relative_to(out.resolve()).as_posix()
            row = {
                "page_id": page_id,
                "source_image": str(img_path.resolve()),
                "scan_file": rel,
                "页码": page_no,
                "章节标题": chapter,
                "file_order": i,
                "line_count": len(lines),
                "recognition": "lecture_scan_vlm",
            }
            if extra:
                row["extra"] = extra
            return (i, row, False)

        if max_workers <= 1:
            for i, img_path in enumerate(images, start=1):
                idx, row, aborted = _run_one(i, img_path)
                if row is None and aborted:
                    cancelled = True
                    break
                if row is not None:
                    _stream_row(idx, row)
                if on_page_done:
                    on_page_done(i, n_img)
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futs = {ex.submit(_run_one, i, p): i for i, p in enumerate(images, start=1)}
                done_count = 0
                for fut in as_completed(futs):
                    idx, row, aborted = fut.result()
                    if row is None and aborted:
                        cancelled = True
                    elif row is not None:
                        _stream_row(idx, row)
                    done_count += 1
                    if on_page_done:
                        on_page_done(done_count, n_img)
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        break

    man_f.close()
    return {
        "n_pages": len(manifest_rows),
        "manifest": str(man_path),
        "out_dir": str(out),
        "model": m_model,
        "max_workers": max_workers,
        "cancelled": cancelled,
    }


def run_lecture_scan_assemble(
    *,
    raw_text: str,
    out_dir: Path,
    config_path: Path | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """阶段 3：LLM 将拼接原文重组为完整题目列表。"""
    cfg = load_config(config_path)
    tl = llm_text_settings()
    if not tl.get("api_key"):
        raise ValueError("未配置 LLM_API_KEY")
    lsc = _scan_cfg(cfg)
    acfg = dict(lsc.get("assemble") or {})
    text_model = resolve_llm_model(cfg, override=model)
    temp = float(acfg.get("temperature", 0.2))
    timeout = float(acfg.get("timeout_seconds", 300.0))
    p_sys, p_user_tpl = lecture_scan_assemble_prompts()
    p_user = p_user_tpl.replace("__RAW_TEXT__", raw_text[:120000])
    cl = OpenAICompatClient(api_key=str(tl.get("api_key")), base_url=tl.get("base_url") or None)
    raw = call_with_retries_timeout(
        lambda t: cl.chat_text_json(
            model=text_model,
            messages=[
                {"role": "system", "content": p_sys},
                {"role": "user", "content": p_user},
            ],
            temperature=temp,
            timeout=float(t),
        ),
        tries=3,
        base_timeout_s=float(timeout),
        base_sleep_s=1.0,
    )
    s = _strip_wrapping_artifacts(raw)
    data = parse_json_object(s)
    items_raw = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items_raw, list):
        raise ValueError("assemble LLM 未返回 items 数组")
    items: list[dict[str, Any]] = []
    for x in items_raw:
        if not isinstance(x, dict):
            continue
        th = str(x.get("题号") or "").strip()
        qt = str(x.get("题目类型") or "").strip() or "未知"
        q = str(x.get("问题") or x.get("stem") or "").replace("\r\n", "\n").strip()
        if not q:
            continue
        items.append({"题号": th, "题目类型": qt, "问题": q})
    out_path = (out_dir / "assembled_questions.json").resolve()
    out_path.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _log.info("lecture-scan assemble: %s 道题 -> %s", len(items), out_path)
    return items


def _items_to_refined_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, it in enumerate(items, start=1):
        th = str(it.get("题号") or "").strip() or str(i)
        q = str(it.get("问题") or "").strip()
        qt = str(it.get("题目类型") or "").strip() or "未知"
        rows.append(
            {
                "题号": th,
                "题目类型": qt,
                "修正状态": "否",
                "原问题": q,
                "原解析": "",
                "问题": q,
                "解析": "",
            }
        )
    return rows


def _resolve_scan_refine_workers(cfg: dict[str, Any], n_tasks: int) -> int:
    raw = (cfg.get("refine") or {}).get("max_workers")
    return cap_workers(n_tasks, raw)


def _run_scan_assemble_core(
    *,
    input_dir: Path,
    out: Path,
    config_path: Path | None,
    vlm_model: str | None,
    assemble_model: str | None,
    cancel_event: threading.Event | None,
    paused: Callable[[], bool] | None,
    on_vlm_page_done: Callable[[int, int], None] | None,
    on_assemble_done: Callable[[], None] | None,
    write_assembled_xlsx: bool = True,
) -> dict[str, Any]:
    """VLM 扫描 → 按页码拼接 → LLM 拆题（两分支共用）。"""
    vlm_sum = run_lecture_scan_vlm(
        input_dir=input_dir,
        out_dir=out,
        config_path=config_path,
        model=vlm_model,
        cancel_event=cancel_event,
        paused=paused,
        on_page_done=on_vlm_page_done,
    )
    if vlm_sum.get("cancelled"):
        return {**vlm_sum, "stage": "vlm_cancelled", "items": [], "refined_rows": []}

    man_path = Path(str(vlm_sum["manifest"]))
    rows = _read_scan_manifest(man_path)
    if not rows:
        raise ValueError("scan manifest 为空")
    sorted_pages = sort_scan_pages(rows)
    chapter_title = resolve_export_chapter_title(sorted_pages)
    work = _manifest_out_root(man_path)
    raw_text = build_concatenated_raw_text(sorted_pages, work=work)
    raw_path = (out / "assembled_raw.txt").resolve()
    raw_path.write_text(raw_text, encoding="utf-8")

    _log.info("scan 阶段2 LLM 拆题：单次请求拼接原文（%s 字符）", len(raw_text))
    items = run_lecture_scan_assemble(
        raw_text=raw_text,
        out_dir=out,
        config_path=config_path,
        model=assemble_model,
    )
    if on_assemble_done:
        on_assemble_done()

    refined_rows = _items_to_refined_rows(items)
    assembled_xlsx = ""
    if write_assembled_xlsx:
        xlsx_path = (out / "assembled_questions.xlsx").resolve()
        write_refined_rows_to_xlsx(
            xlsx_path,
            refined_rows,
            include_lecture_tips=False,
            include_lecture_content=False,
        )
        assembled_xlsx = str(xlsx_path)
    return {
        **vlm_sum,
        "raw_text_file": str(raw_path),
        "n_questions": len(items),
        "assembled_json": str(out / "assembled_questions.json"),
        "assembled_xlsx": assembled_xlsx,
        "章节标题": chapter_title,
        "items": items,
        "refined_rows": refined_rows,
    }


def run_correction_scan_pipeline(
    *,
    input_dir: Path,
    out_dir: Path | None = None,
    config_path: Path | None = None,
    vlm_model: str | None = None,
    assemble_model: str | None = None,
    refine_model: str | None = None,
    cancel_event: threading.Event | None = None,
    paused: Callable[[], bool] | None = None,
    on_vlm_page_done: Callable[[int, int], None] | None = None,
    on_assemble_done: Callable[[], None] | None = None,
    on_refine_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """题目修正分支：与教师讲解相同 scan+assemble，终步为教材向修正（无卷面解析时 LLM 生成）。"""
    root = project_root()
    out = (out_dir or (root / "data" / "out" / "correction_scan")).resolve()
    out.mkdir(parents=True, exist_ok=True)

    core = _run_scan_assemble_core(
        input_dir=input_dir,
        out=out,
        config_path=config_path,
        vlm_model=vlm_model,
        assemble_model=assemble_model,
        cancel_event=cancel_event,
        paused=paused,
        on_vlm_page_done=on_vlm_page_done,
        on_assemble_done=on_assemble_done,
        write_assembled_xlsx=True,
    )
    if core.get("cancelled") or core.get("stage") == "vlm_cancelled":
        return {**core, "stage": "vlm_cancelled"}

    refined_rows = core.get("refined_rows") or []
    chapter_title = str(core.get("章节标题") or "")
    xlsx_name = "correction_result.xlsx"
    if chapter_title:
        safe = sanitize_chapter_title_for_filename(chapter_title)
        if safe:
            xlsx_name = f"{safe}_修正.xlsx"
    out_xlsx = (out / xlsx_name).resolve()

    cfg = load_config(config_path)
    rf_workers = _resolve_scan_refine_workers(cfg, len(refined_rows))
    _log.info("correction-scan 阶段3 教材修正：%s 题，并行 max_workers=%s", len(refined_rows), rf_workers)
    rf_sum = run_refine_on_rows(
        refined_rows,
        out_xlsx=out_xlsx,
        config_path=config_path,
        model=refine_model,
        max_workers=rf_workers,
        on_progress=on_refine_progress,
    )
    return {
        **core,
        "refine": rf_sum,
        "out_xlsx": rf_sum.get("out_xlsx", ""),
        "stage": "complete",
    }


def run_lecture_scan_pipeline(
    *,
    input_dir: Path,
    out_dir: Path | None = None,
    config_path: Path | None = None,
    vlm_model: str | None = None,
    assemble_model: str | None = None,
    lecture_model: str | None = None,
    skip_lecture_content: bool = False,
    cancel_event: threading.Event | None = None,
    paused: Callable[[], bool] | None = None,
    on_vlm_page_done: Callable[[int, int], None] | None = None,
    on_assemble_done: Callable[[], None] | None = None,
    on_lecture_progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """
    教师讲解独立分支：VLM 逐页扫描 → 按页码拼接 → LLM 拆题 → 单题讲课内容 → Word。
    不走 vlm-refine 教材修正。
    """
    root = project_root()
    out = (out_dir or (root / "data" / "out" / "lecture_scan")).resolve()
    out.mkdir(parents=True, exist_ok=True)

    core = _run_scan_assemble_core(
        input_dir=input_dir,
        out=out,
        config_path=config_path,
        vlm_model=vlm_model,
        assemble_model=assemble_model,
        cancel_event=cancel_event,
        paused=paused,
        on_vlm_page_done=on_vlm_page_done,
        on_assemble_done=on_assemble_done,
        write_assembled_xlsx=False,
    )
    if core.get("cancelled") or core.get("stage") == "vlm_cancelled":
        return core

    items = core.get("items") or []
    refined_rows = core.get("refined_rows") or []
    chapter_title = str(core.get("章节标题") or "")

    lc_summary: dict[str, Any] | None = None
    docx_path = lecture_scan_handout_docx_path(out, chapter_title)
    if not skip_lecture_content and items:
        cfg = load_config(config_path)
        lc_workers = _resolve_scan_lecture_workers(cfg, len(items))
        _log.info(
            "lecture-scan 阶段3 讲课内容：%s 道题，并行 max_workers=%s（拆题完成后启动）",
            len(items),
            lc_workers,
        )
        if chapter_title:
            _log.info("lecture-scan 讲义 Word：%s（章节标题=%s）", docx_path.name, chapter_title)
        lc_summary = run_lecture_content_on_refined_rows(
            refined_rows,
            out_docx=docx_path,
            write_stream_csv=False,
            write_xlsx=False,
            write_jsonl=False,
            config_path=config_path,
            model=lecture_model,
            max_workers=lc_workers,
            on_progress=on_lecture_progress,
        )

    return {
        **core,
        "lecture_content": lc_summary,
        "out_docx": str(lc_summary.get("out_docx", "")) if lc_summary else "",
        "stage": "complete",
    }
