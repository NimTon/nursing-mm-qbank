from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from mm_qbank.config import load_config, llm_text_settings, project_root
from mm_qbank.io.csv_out import append_refined_rows_to_csv
from mm_qbank.io.xlsx_out import write_refined_rows_to_xlsx
from mm_qbank.llm.client import OpenAICompatClient
from mm_qbank.pipeline.refine_vlm_run import _normalize_out_item, _parse_refine_response
from mm_qbank.prompts_loader import refine_system, refine_user_payload


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Smoke test: LLM refine one Q/A -> export CSV/XLSX")
    ap.add_argument("--question", default="某患者测量血压时袖带过窄，会导致测量结果偏高还是偏低？", help="question text")
    ap.add_argument("--analysis", default="袖带过窄会导致血压测量值偏低。", help="analysis text (intentionally may be wrong)")
    ap.add_argument("--tihao", default="1", help="题号")
    ap.add_argument("--out-dir", default="data/out/offline_refine_smoketest", help="output directory")
    ap.add_argument("--config", default="", help="optional YAML config path")
    args = ap.parse_args()

    cfg_path = Path(args.config).resolve() if (args.config or "").strip() else None
    cfg = load_config(cfg_path)
    tl = llm_text_settings()
    if not tl.get("api_key"):
        raise ValueError("未配置 LLM_API_KEY：请在 .env 中设置 LLM_BASE_URL 与 LLM_API_KEY")

    rcfg = dict(cfg.get("refine") or {})
    text_model = str(rcfg.get("model") or tl.get("text_model") or "gpt-4o-mini").strip()
    temp = float(rcfg.get("temperature", 0.2))
    timeout_s = float(rcfg.get("timeout_seconds", 180.0))
    web_search = bool(rcfg.get("web_search", False))
    include_lecture_tips = bool(rcfg.get("lecture_tips_with_refine", False))
    export_csv_xlsx_lc = bool(rcfg.get("lecture_content_after_refine", False))

    question = str(args.question or "").strip()
    analysis = str(args.analysis or "").strip()
    tihao = str(args.tihao or "").strip() or "1"

    merged_one: list[dict[str, Any]] = [{"题号": tihao, "问题": question, "解析": analysis}]
    up = refine_user_payload(page_id="offline_smoketest", merged=merged_one, web_search=web_search)
    rsys = refine_system()
    client = OpenAICompatClient(api_key=str(tl["api_key"]), base_url=tl.get("base_url"))

    t0 = time.time()
    raw = client.chat_text_json(
        model=text_model,
        messages=[{"role": "system", "content": rsys}, {"role": "user", "content": up}],
        temperature=temp,
        timeout=timeout_s,
    )
    out_items = _parse_refine_response(raw)
    oi = out_items[0] if out_items else {}

    row = _normalize_out_item({"题号": tihao, "问题": question, "解析": analysis}, oi)
    row["page_id"] = "offline_smoketest"
    row["source_image"] = ""

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = (project_root() / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = (out_dir / "refined_stream.csv").resolve()
    xlsx_path = (out_dir / "refined.xlsx").resolve()
    json_path = (out_dir / "debug.json").resolve()

    append_refined_rows_to_csv(
        csv_path,
        [row],
        include_lecture_tips=include_lecture_tips,
        include_lecture_content=export_csv_xlsx_lc,
    )
    write_refined_rows_to_xlsx(
        xlsx_path,
        [row],
        include_lecture_tips=include_lecture_tips,
        include_lecture_content=export_csv_xlsx_lc,
    )
    json_path.write_text(
        json.dumps(
            {
                "model": text_model,
                "question": question,
                "analysis": analysis,
                "prompt_user": up,
                "llm_raw": raw,
                "row": row,
                "elapsed_s": time.time() - t0,
                "out_csv": str(csv_path),
                "out_xlsx": str(xlsx_path),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"ok": True, "out_csv": str(csv_path), "out_xlsx": str(xlsx_path), "out_debug": str(json_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
