from __future__ import annotations

import logging
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from mm_qbank import prompt_builtins as _fb
from mm_qbank.config import load_config, openai_settings, project_root
from mm_qbank.prompts_loader import get_prompt
from mm_qbank.layout_postprocess.page_text import export_vlm_classified_artifact, write_page_text_manifest
from mm_qbank.llm.client import OpenAICompatClient
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
    return {"题号": n, "类型": t, "内容": c}


def _parse_vlm_response(raw: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    try:
        s = _strip_wrapping_artifacts(raw)
        data = parse_json_object(s)
    except Exception:  # noqa: BLE001
        return [], {"parse_error": "json", "raw_preview": raw[:10000]}

    it: list[Any] | None = None
    if isinstance(data, dict):
        v = data.get("items")
        it = v if isinstance(v, list) else None
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
    return (out, None)


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
) -> dict[str, Any]:
    """
    多模态整页转写为 JSON 分类项（`题号` + `问题`/`解析` + `内容`）及合并 `.txt`；
    输出 ``pages.jsonl`` 中每行带 ``structured_file`` 指向每页 ``.json``，供后处理题号级关联。
    """
    cfg = load_config(config_path)
    oa = openai_settings()
    if not oa.get("api_key"):
        raise ValueError("未配置 API 密钥（OPENAI / DASHSCOPE / 与 DeepSeek 的 DEEPSEEK_API_KEY），无法调用多模态模型")
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
        m_model = str(oa.get("mm_model") or "gpt-4o")
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
    client = OpenAICompatClient(api_key=str(oa.get("api_key")), base_url=oa.get("base_url") or None)
    manifest_rows: list[dict[str, Any]] = []
    cancelled = False
    with tempfile.TemporaryDirectory(prefix="mm_qbank_vlm_") as tmp:
        tdir = Path(tmp)
        for i, img_path in enumerate(images, start=1):
            if _wait_unpaused(pause=paused, cancel=cancel_event):
                _log.info("vlm-text 在页 %s 前被用户取消/结束", i)
                cancelled = True
                break
            page_id = page_id_for(img_path)
            _log.info("[%s/%s] page_id=%s 文件=%s", i, len(images), page_id, img_path.name)
            if use_pre:
                pre_path = tdir / f"{page_id}.png"
                preprocess_image(
                    img_path,
                    pre_path,
                    max_long_edge=max_long_edge,
                    deskew=pre_cfg.get("deskew"),
                    tone_mode=str(pre_cfg.get("tone_mode", "shaded")),
                )
                body = pre_path.read_bytes()
                ctyp = "image/png"
            else:
                body = img_path.read_bytes()
                ctyp = _content_type(img_path.suffix)
            raw = client.chat_vision(
                model=m_model,
                system=p_sys,
                user_text=p_user,
                image_bytes=body,
                content_type=ctyp,
                temperature=temp,
                timeout=timeout,
                response_format_json=True,
            )
            items, extra = _parse_vlm_response(raw)
            if extra:
                _log.warning("page_id=%s VLM 解析需人工检查: %s", page_id, list(extra.keys()))
            _, __, row = export_vlm_classified_artifact(
                work=out,
                page_id=page_id,
                source_image=img_path,
                items=items,
                subdir=subdir,
                extra=extra,
            )
            manifest_rows.append(row)
            if on_page_done is not None:
                on_page_done(i, len(images))
            _log.debug(
                "vlm 分段 segment_count=%s char_count=%s",
                row.get("segment_count"),
                row.get("char_count"),
            )
            if cancel_event is not None and cancel_event.is_set():
                _log.info("vlm-text 在页 %s 之后被用户取消/结束，已写 %s 页", page_id, len(manifest_rows))
                cancelled = True
                break

    man_path = write_page_text_manifest(out, subdir, manifest_name, manifest_rows)
    _log.info("已写入 manifest: %s", man_path)
    return {
        "mode": "vlm",
        "out_dir": str(out),
        "n_pages": len(manifest_rows) if cancelled else len(images),
        "n_total_images": len(images),
        "page_ids": [r["page_id"] for r in manifest_rows],
        "manifest": str(man_path) if man_path else None,
        "model": m_model,
        "cancelled": cancelled,
    }
