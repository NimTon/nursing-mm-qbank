from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from mm_qbank.logging_utils import configure_logging
from mm_qbank.pipeline.lecture_scan_run import run_correction_scan_pipeline, run_lecture_scan_pipeline
from mm_qbank.pipeline.refine_vlm_run import run_refine_from_compose_jsonl, run_refine_vlm_merged
from mm_qbank.pipeline.vlm_text_run import run_vlm_text_only

_log = logging.getLogger(__name__)


def main() -> None:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("-v", "--verbose", action="store_true", help="调试日志 (DEBUG)")
    parent.add_argument("-q", "--quiet", action="store_true", help="仅警告与错误 (WARNING)")
    parser = argparse.ArgumentParser(
        prog="mm-qbank",
        description="护理题库：教师讲解 / 题目修正（scan → assemble → 终步 LLM）；mm-qbank / mm-qbank-gui",
        parents=[parent],
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    lscan = sub.add_parser(
        "lecture-scan",
        parents=[parent],
        help="教师讲解：逐图 VLM → 按页码拼接 → LLM 拆题 → 讲课 Word",
    )
    lscan.add_argument("--in", dest="input_dir", type=Path, required=True, help="输入图片目录（递归扫描）")
    lscan.add_argument(
        "--out-dir",
        dest="out_dir",
        type=Path,
        default=None,
        help="输出根目录（默认 data/out/lecture_scan）",
    )
    lscan.add_argument("--config", dest="config", type=Path, default=None, help="configs/default.yaml")
    lscan.add_argument("--vlm-model", dest="vlm_model", type=str, default=None, help="覆盖 .env 中 VLM_MODEL")
    lscan.add_argument(
        "--assemble-model", dest="assemble_model", type=str, default=None, help="覆盖 .env 中 LLM_MODEL"
    )
    lscan.add_argument(
        "--lecture-model", dest="lecture_model", type=str, default=None, help="覆盖 .env 中 LLM_MODEL"
    )
    lscan.add_argument(
        "--skip-lecture",
        action="store_true",
        help="只做到 LLM 拆题，不生成讲课 Word",
    )

    cscan = sub.add_parser(
        "correction-scan",
        parents=[parent],
        help="题目修正：与 lecture-scan 相同 scan+assemble，终步教材向修正（LLM 生成解析）",
    )
    cscan.add_argument("--in", dest="input_dir", type=Path, required=True, help="输入图片目录（递归扫描）")
    cscan.add_argument(
        "--out-dir",
        dest="out_dir",
        type=Path,
        default=None,
        help="输出根目录（默认 data/out/correction_scan）",
    )
    cscan.add_argument("--config", dest="config", type=Path, default=None, help="configs/default.yaml")
    cscan.add_argument("--vlm-model", dest="vlm_model", type=str, default=None, help="覆盖 .env 中 VLM_MODEL")
    cscan.add_argument(
        "--assemble-model", dest="assemble_model", type=str, default=None, help="覆盖 .env 中 LLM_MODEL"
    )
    cscan.add_argument(
        "--refine-model", dest="refine_model", type=str, default=None, help="覆盖 .env 中 LLM_MODEL"
    )

    vlm = sub.add_parser(
        "vlm-text",
        parents=[parent],
        help="多模态整页转写：每页 json（题号/类型/内容）+ pages.jsonl",
    )
    vlm.add_argument("--in", dest="input_dir", type=Path, required=True, help="输入图片目录（递归扫描）")
    vlm.add_argument(
        "--out-dir",
        dest="out_dir",
        type=Path,
        default=None,
        help="输出根目录（默认 data/out/vlm_text）",
    )
    vlm.add_argument("--config", dest="config", type=Path, default=None, help="configs/default.yaml")
    vlm.add_argument(
        "--model",
        dest="mm_model",
        type=str,
        default=None,
        help="覆盖 .env 中 VLM_MODEL",
    )

    refine = sub.add_parser(
        "vlm-refine",
        parents=[parent],
        help="按题号+题型合并问题/解析，教材向修正并导出 xlsx+jsonl",
    )
    srcg = refine.add_mutually_exclusive_group(required=True)
    srcg.add_argument(
        "--manifest",
        dest="refine_manifest",
        type=Path,
        default=None,
        help="vlm-text 的 pages.jsonl 路径",
    )
    srcg.add_argument(
        "--compose-jsonl",
        dest="compose_jsonl",
        type=Path,
        default=None,
        help="llm-compose 输出的 jsonl",
    )
    refine.add_argument(
        "--out-jsonl",
        dest="refine_jsonl",
        type=Path,
        default=None,
        help="默认 data/out/refine/refined_merged.jsonl",
    )
    refine.add_argument(
        "--out-xlsx",
        dest="refine_xlsx",
        type=Path,
        default=None,
        help="默认 data/out/refine/refined_merged.xlsx",
    )
    refine.add_argument(
        "--out-csv",
        dest="refine_csv",
        type=Path,
        default=None,
        help="流式 CSV，默认 refined_merged_stream.csv",
    )
    refine.add_argument("--config", dest="refine_config", type=Path, default=None, help="configs/default.yaml")
    refine.add_argument(
        "--model",
        dest="refine_model",
        type=str,
        default=None,
        help="覆盖 .env 中 LLM_MODEL",
    )

    verify = sub.add_parser(
        "verify-config",
        parents=[parent],
        help="各发一次请求：VLM 描述测试图、LLM 发「你好」，校验网关/密钥/模型（产生少量计费）",
    )
    verify.add_argument("--config", dest="verify_config", type=Path, default=None, help="configs/default.yaml")
    verify.add_argument("--vlm-only", action="store_true", help="只测 VLM")
    verify.add_argument("--llm-only", action="store_true", help="只测 LLM")
    verify.add_argument("--vlm-model", type=str, default=None, help="覆盖 VLM 模型名")
    verify.add_argument("--llm-model", type=str, default=None, help="覆盖 LLM 模型名")
    verify.add_argument("--vlm-timeout", type=float, default=90.0)
    verify.add_argument("--llm-timeout", type=float, default=45.0)

    args = parser.parse_args()
    configure_logging(verbose=bool(args.verbose), quiet=bool(args.quiet))
    _log.info("子命令: %s", args.cmd)

    if args.cmd == "lecture-scan":
        summary = run_lecture_scan_pipeline(
            input_dir=args.input_dir,
            out_dir=args.out_dir,
            config_path=args.config,
            vlm_model=args.vlm_model,
            assemble_model=args.assemble_model,
            lecture_model=args.lecture_model,
            skip_lecture_content=bool(args.skip_lecture),
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.cmd == "correction-scan":
        summary = run_correction_scan_pipeline(
            input_dir=args.input_dir,
            out_dir=args.out_dir,
            config_path=args.config,
            vlm_model=args.vlm_model,
            assemble_model=args.assemble_model,
            refine_model=args.refine_model,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.cmd == "vlm-text":
        summary = run_vlm_text_only(
            input_dir=args.input_dir,
            out_dir=args.out_dir,
            config_path=args.config,
            model=args.mm_model,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.cmd == "vlm-refine":
        if args.compose_jsonl is not None:
            summary = run_refine_from_compose_jsonl(
                compose_jsonl=args.compose_jsonl,
                out_jsonl=args.refine_jsonl,
                out_csv=args.refine_csv,
                out_xlsx=args.refine_xlsx,
                config_path=args.refine_config,
                model=args.refine_model,
            )
        else:
            summary = run_refine_vlm_merged(
                manifest_path=args.refine_manifest,
                out_jsonl=args.refine_jsonl,
                out_csv=args.refine_csv,
                out_xlsx=args.refine_xlsx,
                config_path=args.refine_config,
                model=args.refine_model,
            )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.cmd == "verify-config":
        try:
            from mm_qbank.api_verify import run_verify  # type: ignore
        except ModuleNotFoundError as e:
            raise SystemExit(
                "缺少可选模块 `mm_qbank.api_verify`：当前工程无法运行 `verify-config`。\n"
                "如果你不需要该命令，可忽略；若需要，请从上游同步/恢复 `src/mm_qbank/api_verify.py`。"
            ) from e
        summary = run_verify(
            config_path=args.verify_config,
            vlm_model=args.vlm_model,
            llm_model=args.llm_model,
            vlm_only=args.vlm_only,
            llm_only=args.llm_only,
            vlm_timeout=args.vlm_timeout,
            llm_timeout=args.llm_timeout,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if not summary.get("all_ok"):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
