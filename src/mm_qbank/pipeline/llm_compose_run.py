from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mm_qbank import prompt_builtins as _fb
from mm_qbank.config import load_config, text_llm_settings
from mm_qbank.prompts_loader import get_prompt
from mm_qbank.llm.client import OpenAICompatClient
from mm_qbank.llm.jsonutil import parse_json_object
from mm_qbank.pipeline.vlm_text_run import _wait_unpaused

_log = logging.getLogger(__name__)


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
    tl = text_llm_settings()
    if not tl.get("api_key"):
        raise ValueError(
            "未配置文本 LLM 密钥：请设 OPENAI / DASHSCOPE，或与百炼并存时设 DEEPSEEK_API_KEY / TEXT_LLM_*，见 config.text_llm_settings"
        )

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
    # 优先级：CLI/GUI 参数 > configs 中 llm.model > text_llm_settings（可与 VLM 网关分离）
    text_model = m_arg or m_cfg or str(tl.get("text_model") or "gpt-4o-mini")

    system_z = get_prompt("llm_compose_system.txt", _fb.LLM_COMPOSE_SYSTEM)
    user_tpl = get_prompt("llm_compose_user.txt", _fb.LLM_COMPOSE_USER)
    client = OpenAICompatClient(api_key=str(tl["api_key"]), base_url=tl.get("base_url") or None)
    rows = _read_manifest_lines(mpath)
    _log.info("manifest 共 %s 页，模型=%s temperature=%s", len(rows), text_model, temperature)
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)

    n_ok = 0
    cancelled = False
    n_rows = len(rows)
    with out_jsonl.open("w", encoding="utf-8") as fout:
        for idx, row in enumerate(rows, start=1):
            if _wait_unpaused(pause=paused, cancel=cancel_event):
                _log.info("llm-compose 在页 %s 前被用户取消/结束", idx)
                cancelled = True
                break
            page_id = str(row.get("page_id", ""))
            rel = str(row.get("text_file", "")).strip().replace("\\", "/")
            tpath = (root / rel).resolve()
            if not tpath.is_file():
                _log.warning("[%s/%s] page_id=%s 跳过：找不到文本 %s", idx, len(rows), page_id, tpath)
                fout.write(
                    json.dumps(
                        {"page_id": page_id, "error": f"找不到文本文件: {tpath}", "items": []},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            else:
                page_plain = tpath.read_text(encoding="utf-8")
                _log.info(
                    "[%s/%s] page_id=%s 请求 LLM（文本长度=%s 字符）",
                    idx,
                    len(rows),
                    page_id,
                    len(page_plain),
                )
                raw = client.chat_text_json(
                    model=text_model,
                    messages=[
                        {"role": "system", "content": system_z},
                        {"role": "user", "content": user_tpl.replace("__PAGE_PLAIN__", page_plain)},
                    ],
                    temperature=temperature,
                )
                data = parse_json_object(raw)
                items = data.get("items")
                if not isinstance(items, list):
                    items = []
                _log.info("[%s/%s] page_id=%s 解析得到 items=%s 条", idx, len(rows), page_id, len(items))
                out_row = {
                    "page_id": page_id,
                    "source_image": row.get("source_image"),
                    "items": items,
                }
                fout.write(json.dumps(out_row, ensure_ascii=False) + "\n")
                n_ok += 1
            if on_page_done is not None and n_rows > 0:
                on_page_done(idx, n_rows)
            if cancel_event is not None and cancel_event.is_set():
                _log.info("llm-compose 在页 %s 之后被用户取消/结束", page_id)
                cancelled = True
                break

    _log.info("完成：写入 %s 行 -> %s", n_ok, out_jsonl.resolve())
    return {
        "manifest": str(mpath),
        "out_jsonl": str(out_jsonl.resolve()),
        "n_pages": len(rows),
        "n_written": n_ok,
        "model": text_model,
        "cancelled": cancelled,
    }
