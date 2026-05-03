from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from mm_qbank.logging_utils import configure_logging
from mm_qbank.pipeline.lecture_content_run import run_lecture_content_from_xlsx
from mm_qbank.pipeline.lecture_tips_run import run_lecture_tips_from_xlsx
from mm_qbank.pipeline.llm_compose_run import run_llm_compose_manifest
from mm_qbank.pipeline.refine_vlm_run import run_refine_from_compose_jsonl, run_refine_vlm_merged
from mm_qbank.pipeline.vlm_text_run import run_vlm_text_only

_log = logging.getLogger(__name__)


def main() -> None:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("-v", "--verbose", action="store_true", help="调试日志 (DEBUG)")
    parent.add_argument("-q", "--quiet", action="store_true", help="仅警告与错误 (WARNING)")
    parser = argparse.ArgumentParser(
        prog="mm-qbank",
        description="护理题库：VLM 转写 / 教材修正 / xlsx 讲师提醒与讲课内容（Word）；流式 CSV 断点续跑（mm-qbank / mm-qbank-gui）",
        parents=[parent],
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    # 将 parent 也挂到子命令上，否则子命令之后出现 -v/-q 会报 unrecognized
    vlm = sub.add_parser(
        "vlm-text",
        parents=[parent],
        help="多模态大模型整页转写，输出 .json + .txt + pages.jsonl",
    )
    vlm.add_argument("--in", dest="input_dir", type=Path, required=True, help="输入图片目录（递归扫描）")
    vlm.add_argument(
        "--out-dir",
        dest="out_dir",
        type=Path,
        default=None,
        help="输出根目录（默认：项目下 data/out/vlm_text）",
    )
    vlm.add_argument("--config", dest="config", type=Path, default=None, help="覆盖默认 configs/default.yaml")
    vlm.add_argument(
        "--model",
        dest="mm_model",
        type=str,
        default=None,
        help="覆盖多模态模型名（缺省为配置 vlm.model 或环境变量 VLM_MODEL）",
    )

    refine = sub.add_parser(
        "vlm-refine",
        parents=[parent],
        help="读 pages.jsonl 或 llm_compose_merged.jsonl，按题号合并问题+解析，再 LLM 对照护理学教材修正，导出 xlsx+jsonl",
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
        help="llm-compose 的输出 jsonl（如 vlm_gui_*/llm_compose_merged.jsonl）",
    )
    refine.add_argument(
        "--out-jsonl",
        dest="refine_jsonl",
        type=Path,
        default=None,
        help="每题一行 JSON 的 jsonl（默认 data/out/refine/refined_merged.jsonl）",
    )
    refine.add_argument(
        "--out-xlsx",
        dest="refine_xlsx",
        type=Path,
        default=None,
        help="Excel 表（默认 data/out/refine/refined_merged.xlsx）",
    )
    refine.add_argument(
        "--out-csv",
        dest="refine_csv",
        type=Path,
        default=None,
        help="流式追加 CSV（默认 data/out/refine/refined_merged_stream.csv）",
    )
    refine.add_argument("--config", dest="refine_config", type=Path, default=None, help="configs/default.yaml")
    refine.add_argument(
        "--model",
        dest="refine_model",
        type=str,
        default=None,
        help="覆盖 refine.model 或环境变量 LLM_MODEL",
    )

    llm = sub.add_parser(
        "llm-compose",
        parents=[parent],
        help="读取 pages.jsonl 与 .txt 整页文本，按页拆成结构化题目 JSONL",
    )
    llm.add_argument(
        "--manifest",
        dest="manifest",
        type=Path,
        required=True,
        help="vlm-text 的 pages.jsonl 路径，如 data/out/vlm_text/pages/pages.jsonl",
    )
    llm.add_argument("--out", dest="out_jsonl", type=Path, required=True, help="输出每页一行 JSON 的 .jsonl")
    llm.add_argument("--config", dest="config", type=Path, default=None, help="覆盖默认 configs/default.yaml")
    llm.add_argument("--model", dest="model", type=str, default=None, help="覆盖环境变量 LLM_MODEL")

    ltip = sub.add_parser(
        "xlsx-lecture-tips",
        parents=[parent],
        help="从结果 xlsx 批量生成「讲师提醒」列，流式 CSV 断点续跑，另存新表",
    )
    ltip.add_argument(
        "--in-xlsx",
        dest="in_xlsx",
        type=Path,
        required=True,
        help="已导出的结果表（如 refined_merged.xlsx，含题号/原或修正后问题与解析等）",
    )
    ltip.add_argument(
        "--out-xlsx",
        dest="out_xlsx",
        type=Path,
        default=None,
        help="输出 xlsx（默认同目录：原名_lecture_tips.xlsx）",
    )
    ltip.add_argument(
        "--out-csv",
        dest="lecture_csv",
        type=Path,
        default=None,
        help="流式追加 CSV（默认同目录：原名_lecture_tips_stream.csv）",
    )
    ltip.add_argument(
        "--out-jsonl",
        dest="lecture_jsonl",
        type=Path,
        default=None,
        help="本运行追加产生的 jsonl（仅含本次新写的行，默认同目录）",
    )
    ltip.add_argument(
        "--config", dest="lecture_config", type=Path, default=None, help="覆盖 configs/default.yaml"
    )
    ltip.add_argument(
        "--model", dest="lecture_model", type=str, default=None, help="覆盖 lecture_tips.model 或 LLM_MODEL"
    )

    lcnt = sub.add_parser(
        "xlsx-lecture-content",
        parents=[parent],
        help="从结果 xlsx 批量生成「要点」「讲课内容」，流式 CSV 断点续跑，另存 xlsx + Word 讲义",
    )
    lcnt.add_argument(
        "--in-xlsx",
        dest="lc_in_xlsx",
        type=Path,
        required=True,
        help="已导出的结果表（与 refined 表结构兼容）",
    )
    lcnt.add_argument(
        "--out-xlsx",
        dest="lc_out_xlsx",
        type=Path,
        default=None,
        help="输出 xlsx（默认同目录：原名_lecture_content.xlsx）",
    )
    lcnt.add_argument(
        "--out-csv",
        dest="lc_csv",
        type=Path,
        default=None,
        help="流式追加 CSV（默认同目录：原名_lecture_content_stream.csv）",
    )
    lcnt.add_argument(
        "--out-jsonl",
        dest="lc_jsonl",
        type=Path,
        default=None,
        help="汇总 jsonl（默认同目录）",
    )
    lcnt.add_argument(
        "--config", dest="lc_config", type=Path, default=None, help="覆盖 configs/default.yaml"
    )
    lcnt.add_argument(
        "--model",
        dest="lc_model",
        type=str,
        default=None,
        help="覆盖 lecture_content.model 或 LLM_MODEL",
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

    if args.cmd == "vlm-text":
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
    elif args.cmd == "llm-compose":
        summary = run_llm_compose_manifest(
            manifest_path=args.manifest,
            out_jsonl=args.out_jsonl,
            config_path=args.config,
            model=args.model,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.cmd == "xlsx-lecture-tips":
        summary = run_lecture_tips_from_xlsx(
            in_xlsx=args.in_xlsx,
            out_xlsx=args.out_xlsx,
            out_csv=args.lecture_csv,
            out_jsonl=args.lecture_jsonl,
            config_path=args.lecture_config,
            model=args.lecture_model,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.cmd == "xlsx-lecture-content":
        summary = run_lecture_content_from_xlsx(
            in_xlsx=args.lc_in_xlsx,
            out_xlsx=args.lc_out_xlsx,
            out_csv=args.lc_csv,
            out_jsonl=args.lc_jsonl,
            config_path=args.lc_config,
            model=args.lc_model,
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
