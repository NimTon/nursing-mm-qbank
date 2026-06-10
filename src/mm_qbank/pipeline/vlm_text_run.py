from __future__ import annotations

import logging
import os
import json
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from mm_qbank import prompt_builtins as _fb
from mm_qbank.config import load_config, project_root, vlm_settings
from mm_qbank.prompts_loader import get_prompt
from mm_qbank.layout_postprocess.page_text import export_vlm_classified_artifact, write_page_text_manifest
from mm_qbank.llm.client import OpenAICompatClient
from mm_qbank.llm.retry import call_with_retries_timeout
from mm_qbank.llm.jsonutil import parse_json_object
from mm_qbank.pipeline.scan_pages import list_input_images, page_id_for
from mm_qbank.preprocess.image import preprocess_image

_log = logging.getLogger(__name__)

def _content_type(suffix: str) -> str:
    s = suffix.lower()
    if s == ".png":
        return "image/png"
    if s in (".jpg", ".jpeg"):
        return "image/jpeg"
    if s == ".bmp":
        return "image/bmp"
    if s == ".webp":
        return "image/webp"
    return "image/png"


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


def _resolve_max_long_edge(*, vlm: dict[str, Any], pre_cfg: dict[str, Any]) -> int:
    if "max_long_edge" in vlm and vlm["max_long_edge"] is not None:
        return int(vlm["max_long_edge"])
    m = pre_cfg.get("max_long_edge", 2000)
    return int(m) if m is not None else 2000


def _normalize_segment(raw: dict[str, Any]) -> dict[str, Any]:
    n_raw = raw.get("题号", "")
    n = str(n_raw).strip() if n_raw is not None else ""
    t = str(raw.get("类型", "")).strip()
    if t not in ("问题", "解析"):
        t_lower = t.lower()
        if t_lower in ("question", "q", "stem", "题"):
            t = "问题"
        elif t_lower in ("analysis", "explanation", "析") or t in ("解", "解析", "答析"):
            t = "解析"
        else:
            t = "问题"
    c_raw = raw.get("内容", raw.get("content", ""))
    if isinstance(c_raw, str):
        c = c_raw.replace("\r\n", "\n").strip()
    else:
        c = str(c_raw).strip()
    qt_raw = raw.get("题目类型") or raw.get("题型") or raw.get("试题类型")
    qt = str(qt_raw).strip() if qt_raw is not None else ""
    out: dict[str, Any] = {"题号": n, "类型": t, "内容": c}
    if qt:
        out["题目类型"] = qt
    return out


def _parse_vlm_response(raw: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    try:
        s = _strip_wrapping_artifacts(raw)
        data = parse_json_object(s)
    except Exception:  # noqa: BLE001
        return [], {"parse_error": "json", "raw_preview": raw[:10000]}

    it: list[Any] | None = None
    composed: list[Any] | None = None
    if isinstance(data, dict):
        v = data.get("items")
        it = v if isinstance(v, list) else None
        v2 = data.get("composed_items")
        composed = v2 if isinstance(v2, list) else None
    elif isinstance(data, list):
        it = data

    if it is None:
        return [], {"parse_error": "no_items", "raw_preview": raw[:10000]}

    out: list[dict[str, Any]] = []
    for x in it:
        if not isinstance(x, dict):
            continue
        d = cast(dict[str, Any], x)
        out.append(_normalize_segment(d))
    extra: dict[str, Any] | None = None
    if composed is not None:
        # 透传给 structured_file，供后续离线 llm-compose 提取
        extra = {"composed_items": composed}
    return (out, extra)


def _cap_workers(n_tasks: int, raw: Any) -> int:
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        w = min(32, (os.cpu_count() or 4) * 4)
    else:
        w = int(raw)
    w = max(1, w)
    return min(w, max(1, n_tasks))


def _wait_unpaused(
    *, pause: Callable[[], bool] | None, cancel: threading.Event | None
) -> bool:
    """在「每页开始」时调用：若应中止（用户点结束/取消）返回 True；若处于暂停则阻塞至继续或取消。"""
    while True:
        if cancel is not None and cancel.is_set():
            return True
        if pause is None or not pause():
            return False
        time.sleep(0.12)


def run_vlm_text_only(
    *,
    input_dir: Path,
    out_dir: Path | None = None,
    config_path: Path | None = None,
    model: str | None = None,
    cancel_event: threading.Event | None = None,
    paused: Callable[[], bool] | None = None,
    on_page_done: Callable[[int, int], None] | None = None,
    on_page_result: Callable[
        [int, int, str, Path, list[dict[str, Any]], dict[str, Any] | None, dict[str, Any]], None
    ]
    | None = None,
) -> dict[str, Any]:
    """
    多模态整页转写为 JSON 分类项（`题号` + `问题`/`解析` + `内容`）及合并 `.txt`；
    输出 ``pages.jsonl`` 中每行带 ``structured_file`` 指向每页 ``.json``，供后处理题号级关联。

    同一步内可并行请求多张图（`vlm.max_workers`），顺序与 `pages.jsonl` 仍与文件扫描序一致；下游 llm-compose / vlm-refine
    须在**本函数全部写完后**由调用方再执行。
    """
    cfg = load_config(config_path)
    vs = vlm_settings()
    if not vs.get("api_key"):
        raise ValueError("未配置 VLM_API_KEY：请在 .env 中设置 VLM_BASE_URL 与 VLM_API_KEY")
    vlm = dict(cfg.get("vlm") or {})
    pre_cfg = dict(cfg.get("preprocess") or {})
    pt = dict(cfg.get("page_text") or {})
    subdir = str(pt.get("subdir", "pages"))
    manifest_name = str(pt.get("manifest", "pages.jsonl"))
    if (model or "").strip():
        m_model = (model or "").strip()
    elif (vlm.get("model") is not None) and str(vlm.get("model", "")).strip():
        m_model = str(vlm.get("model")).strip()
    else:
        m_model = str(vs.get("mm_model") or "gpt-4o")
    temp = float(vlm.get("temperature", 0.1))
    timeout = float(vlm.get("timeout_seconds", 300.0))
    use_pre = bool(vlm.get("use_preprocess", True))

    root = project_root()
    out = (out_dir or (root / "data" / "out" / "vlm_text")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    _log.info("vlm-text 输入: %s", input_dir.resolve())
    _log.info("vlm-text 输出: %s", out)
    _log.info("vlm-text 模型: %s temperature=%s", m_model, temp)

    max_long_edge = _resolve_max_long_edge(vlm=vlm, pre_cfg=pre_cfg)
    if config_path:
        _log.debug("配置文件: %s", config_path.resolve())

    images = list_input_images(input_dir)
    if not images:
        raise FileNotFoundError(f"目录中未找到图片: {input_dir}")
    _log.info("待处理: %s 张", len(images))

    p_sys = get_prompt("vlm_system.txt", _fb.VLM_SYSTEM)
    p_user = get_prompt("vlm_user.txt", _fb.VLM_USER)
    api_key = str(vs.get("api_key"))
    base_url = vs.get("base_url") or None
    n_img = len(images)
    max_workers = _cap_workers(n_img, vlm.get("max_workers"))
    _log.info("VLM 并行：max_workers=%s（%s 张图）", max_workers, n_img)

    manifest_rows: list[dict[str, Any]] = []
    cancelled = False
    # 流式写 manifest：每页完成就落盘，防止中途卡死/中断丢失
    man_path = (out / subdir / manifest_name).resolve()
    man_path.parent.mkdir(parents=True, exist_ok=True)
    # 每次运行默认覆写，避免与旧内容混杂；若要断点续跑可再扩展为 append+去重
    man_f = man_path.open("w", encoding="utf-8", newline="")
    _man_lock = threading.Lock()
    _pending_rows: dict[int, dict[str, Any]] = {}
    _next_write_idx = 1

    def _stream_manifest_row(idx: int, row: dict[str, Any]) -> None:
        nonlocal _next_write_idx
        with _man_lock:
            _pending_rows[idx] = row
            while _next_write_idx in _pending_rows:
                r = _pending_rows.pop(_next_write_idx)
                man_f.write(json.dumps(r, ensure_ascii=False) + "\n")
                man_f.flush()
                try:
                    os.fsync(man_f.fileno())
                except OSError:
                    # 某些文件系统/环境可能不支持；flush 已足够保证大部分场景的可恢复性
                    pass
                manifest_rows.append(r)
                _log.info(
                    "vlm-text stream-saved [%s/%s] page_id=%s -> %s",
                    _next_write_idx,
                    n_img,
                    r.get("page_id", ""),
                    str(man_path),
                )
                _next_write_idx += 1

    with tempfile.TemporaryDirectory(prefix="mm_qbank_vlm_") as tmp:
        tdir = Path(tmp)
        _prog_lock = threading.Lock()
        _n_done = 0

        def _run_one(i: int, img_path: Path) -> tuple[int, dict[str, Any] | None, bool]:
            """(序号, manifest 行或略过, 是否因取消/暂停在请求前中止)"""
            if cancel_event is not None and cancel_event.is_set():
                return (i, None, True)
            if _wait_unpaused(pause=paused, cancel=cancel_event):
                return (i, None, True)
            page_id = page_id_for(img_path)
            _log.info("[%s/%s] page_id=%s 文件=%s", i, n_img, page_id, img_path.name)
            if use_pre:
                pre_path = tdir / f"{page_id}.png"
                preprocess_image(
                    img_path,
                    pre_path,
                    max_long_edge=max_long_edge,
                    deskew=pre_cfg.get("deskew"),
                    tone_mode=str(pre_cfg.get("tone_mode", "shaded")),
                    rotate_degrees=float(pre_cfg.get("rotate_degrees", 0.0) or 0.0),
                    auto_exif=bool(pre_cfg.get("auto_exif", True)),
                    auto_rotate=bool(pre_cfg.get("auto_rotate", False)),
                )
                body = pre_path.read_bytes()
                ctyp = "image/png"
            else:
                body = img_path.read_bytes()
                ctyp = _content_type(img_path.suffix)
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
                on_retry=lambda att, e, s, nt: _log.warning(
                    "VLM HTTP 失败将重试 page_id=%s attempt=%s/3 sleep=%.2fs next_timeout=%.1fs err=%s: %s",
                    page_id,
                    att + 1,
                    s,
                    nt,
                    type(e).__name__,
                    e,
                ),
            )
            items, extra = _parse_vlm_response(raw)
            if extra:
                _log.warning("page_id=%s VLM 解析需人工检查: %s", page_id, list(extra.keys()))
            if not items and not extra:
                _log.warning(
                    "page_id=%s VLM 返回空 items：请检查 vlm.model / VLM_MODEL 是否为支持读图的多模态模型；"
                    "原始回复前 800 字：%s",
                    page_id,
                    (raw or "")[:800],
                )
            _, __, row = export_vlm_classified_artifact(
                work=out,
                page_id=page_id,
                source_image=img_path,
                items=items,
                subdir=subdir,
                extra=extra,
            )
            if on_page_result is not None:
                try:
                    on_page_result(i, n_img, page_id, img_path, items, extra, row)
                except Exception as e:  # noqa: BLE001
                    _log.warning("on_page_result 回调失败 page_id=%s: %s", page_id, e)
            if cancel_event is not None and cancel_event.is_set():
                return (i, None, True)
            _log.debug(
                "vlm 分段 segment_count=%s char_count=%s",
                row.get("segment_count"),
                row.get("char_count"),
            )
            return (i, row, False)

        if max_workers <= 1:
            for i, img_path in enumerate(images, start=1):
                idx, row, aborted = _run_one(i, img_path)
                if row is None and aborted:
                    cancelled = True
                    _log.info("vlm-text 在序号 %s 前中止", i)
                    break
                if row is not None:
                    _stream_manifest_row(idx, row)
                if on_page_done is not None:
                    on_page_done(i, n_img)
                if cancel_event is not None and cancel_event.is_set() and row is not None:
                    cancelled = True
                    _log.info("vlm-text 在页完成后检测到取消，已写 %s 页", len(manifest_rows))
                    break
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                pending: dict[Any, int] = {
                    ex.submit(_run_one, i, p): i
                    for i, p in enumerate(images, start=1)
                }
                try:
                    for fut in as_completed(pending):
                        try:
                            _idx, row, _ab = fut.result()
                        except Exception:  # noqa: BLE001
                            ex.shutdown(wait=False, cancel_futures=True)
                            raise
                        if row is not None:
                            _stream_manifest_row(_idx, row)
                        if on_page_done is not None:
                            with _prog_lock:
                                _n_done += 1
                                c = _n_done
                            on_page_done(c, n_img)
                except Exception:  # noqa: BLE001
                    raise
            if len(manifest_rows) < n_img and cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _log.info("vlm-text 因取消/中止，已收齐 %s/%s 页", len(manifest_rows), n_img)

    try:
        man_f.close()
    except OSError:
        pass
    # 正常结束后再次覆写一遍（保证完整性/顺序一致）；若中途卡死/崩溃，流式文件也已包含已完成页
    final_man_path = write_page_text_manifest(out, subdir, manifest_name, manifest_rows)
    _log.info("已写入 manifest: %s", final_man_path or man_path)
    return {
        "mode": "vlm",
        "out_dir": str(out),
        "n_pages": len(manifest_rows) if cancelled else len(images),
        "n_total_images": len(images),
        "page_ids": [r["page_id"] for r in manifest_rows],
        "manifest": str(final_man_path or man_path),
        "model": m_model,
        "cancelled": cancelled,
    }
