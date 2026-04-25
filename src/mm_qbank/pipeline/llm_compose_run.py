from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mm_qbank import prompt_builtins as _fb
from mm_qbank.config import load_config, llm_text_settings
from mm_qbank.prompts_loader import get_prompt
from mm_qbank.llm.client import OpenAICompatClient
from mm_qbank.llm.jsonutil import parse_json_object
from mm_qbank.pipeline.vlm_text_run import _wait_unpaused

_log = logging.getLogger(__name__)


def _cap_workers(n_tasks: int, raw: Any) -> int:
    if raw is None or (isinstance(raw, str) and not str(raw).strip()):
        w = min(32, (os.cpu_count() or 4) * 4)
    else:
        w = int(raw)
    w = max(1, w)
    return min(w, max(1, n_tasks))


def _manifest_out_root(manifest_path: Path) -> Path:
    """manifest 位于 {out}/pages/pages.jsonl → 返回 {out}。"""
    return manifest_path.resolve().parent.parent


def _read_manifest_lines(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def run_llm_compose_manifest(
    *,
    manifest_path: Path,
    out_jsonl: Path,
    config_path: Path | None = None,
    model: str | None = None,
    cancel_event: threading.Event | None = None,
    paused: Callable[[], bool] | None = None,
    on_page_done: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """
    读取 ``pages.jsonl`` 与每页 ``text_file`` 纯文本，按页调用在线模型将整页文拆成结构化题目，写入 ``out_jsonl``（每行一页的 JSON）。
    """
    cfg = load_config(config_path)
    tl = llm_text_settings()
    if not tl.get("api_key"):
        raise ValueError("未配置 LLM_API_KEY：请在 .env 中设置 LLM_BASE_URL 与 LLM_API_KEY")

    mpath = manifest_path.resolve()
    if not mpath.is_file():
        raise FileNotFoundError(f"未找到 manifest: {mpath}")

    root = _manifest_out_root(mpath)
    _log.info("llm-compose manifest: %s", mpath)
    _log.info("文本根目录: %s", root)
    _log.info("llm-compose 文本网关: %s", tl.get("base_url") or "(默认官方)")
    llm_cfg = dict(cfg.get("llm") or {})
    temperature = float(llm_cfg.get("temperature", 0.15))
    m_yaml = llm_cfg.get("model")
    m_cfg = str(m_yaml).strip() if m_yaml is not None and str(m_yaml).strip() else None
    m_arg = (model or "").strip() or None
    # 优先级：CLI/GUI 参数 > configs 中 llm.model > llm_text_settings（LLM_BASE_URL/KEY 与 VLM 可分离）
    text_model = m_arg or m_cfg or str(tl.get("text_model") or "gpt-4o-mini")

    system_z = get_prompt("llm_compose_system.txt", _fb.LLM_COMPOSE_SYSTEM)
    user_tpl = get_prompt("llm_compose_user.txt", _fb.LLM_COMPOSE_USER)
    rows = _read_manifest_lines(mpath)
    _log.info("manifest 共 %s 页，模型=%s temperature=%s", len(rows), text_model, temperature)
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)

    n_ok = 0
    cancelled = False
    n_rows = len(rows)
    if n_rows < 1:
        out_jsonl.write_text("", encoding="utf-8")
        return {
            "manifest": str(mpath),
            "out_jsonl": str(out_jsonl.resolve()),
            "n_pages": 0,
            "n_written": 0,
            "model": text_model,
            "cancelled": False,
        }

    max_w = _cap_workers(n_rows, llm_cfg.get("max_workers"))
    _log.info("llm-compose 并行：max_workers=%s", max_w)
    _prog_lock = threading.Lock()
    _n_done = 0
    out_lines: list[str] = [""] * n_rows
    key = str(tl["api_key"])
    burl = tl.get("base_url") or None

    def _compose_line(j0: int, row: dict[str, Any]) -> tuple[int, str, bool, bool]:
        """
        返回 (下标, json 行, 是否因取消/暂停未产出, 是否计为成功调用了 LLM)。
        行始终非空（含缺文件错误）；取消时第三元为 True、第二元为空。
        """
        if cancel_event is not None and cancel_event.is_set():
            return (j0, "", True, False)
        if _wait_unpaused(pause=paused, cancel=cancel_event):
            return (j0, "", True, False)
        idx = j0 + 1
        page_id = str(row.get("page_id", ""))
        rel = str(row.get("text_file", "")).strip().replace("\\", "/")
        tpath = (root / rel).resolve()
        if not tpath.is_file():
            _log.warning("[%s/%s] page_id=%s 跳过：找不到文本 %s", idx, n_rows, page_id, tpath)
            line = json.dumps(
                {"page_id": page_id, "error": f"找不到文本文件: {tpath}", "items": []},
                ensure_ascii=False,
            )
            return (j0, line, False, False)
        page_plain = tpath.read_text(encoding="utf-8")
        _log.info(
            "[%s/%s] page_id=%s 请求 LLM（文本长度=%s 字符）",
            idx,
            n_rows,
            page_id,
            len(page_plain),
        )
        c = OpenAICompatClient(api_key=key, base_url=burl)
        raw2 = c.chat_text_json(
            model=text_model,
            messages=[
                {"role": "system", "content": system_z},
                {"role": "user", "content": user_tpl.replace("__PAGE_PLAIN__", page_plain)},
            ],
            temperature=temperature,
        )
        data = parse_json_object(raw2)
        items = data.get("items")
        if not isinstance(items, list):
            items = []
        _log.info("[%s/%s] page_id=%s 解析得到 items=%s 条", idx, n_rows, page_id, len(items))
        out_row = {
            "page_id": page_id,
            "source_image": row.get("source_image"),
            "items": items,
        }
        line = json.dumps(out_row, ensure_ascii=False)
        return (j0, line, False, True)

    if max_w <= 1:
        for j0, row in enumerate(rows):
            j0, line, abrt, is_ok = _compose_line(j0, row)
            if abrt:
                cancelled = True
                _log.info("llm-compose 在页前被用户取消/结束")
                break
            out_lines[j0] = line
            if is_ok:
                n_ok += 1
            if on_page_done is not None and n_rows > 0:
                on_page_done(j0 + 1, n_rows)
    else:
        with ThreadPoolExecutor(max_workers=max_w) as ex:
            futs = [ex.submit(_compose_line, j0, row) for j0, row in enumerate(rows)]
            try:
                for fut in as_completed(futs):
                    j0, line, abrt, is_ok = fut.result()
                    if not line and abrt:
                        cancelled = True
                    if line:
                        out_lines[j0] = line
                        if is_ok:
                            n_ok += 1
                    if on_page_done is not None and n_rows > 0:
                        with _prog_lock:
                            _n_done += 1
                            c = _n_done
                        on_page_done(c, n_rows)
            except Exception:  # noqa: BLE001
                ex.shutdown(wait=False, cancel_futures=True)
                raise
        if not cancelled and cancel_event is not None and cancel_event.is_set() and n_ok < n_rows:
            cancelled = True
            _log.info("llm-compose 部分页未跑完（与取消/暂停有关）")

    with out_jsonl.open("w", encoding="utf-8") as fout2:
        for s in out_lines:
            if s:
                fout2.write(s + "\n")

    n_out_lines = sum(1 for s in out_lines if s)
    _log.info("完成：成功调 LLM 的页=%s，写出非空行=%s -> %s", n_ok, n_out_lines, out_jsonl.resolve())
    return {
        "manifest": str(mpath),
        "out_jsonl": str(out_jsonl.resolve()),
        "n_pages": len(rows),
        "n_written": n_ok,
        "model": text_model,
        "cancelled": cancelled,
    }
