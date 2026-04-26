from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def _has_any_image(root: Path) -> bool:
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in IMG_EXTS:
            return True
    return False


def _safe_name(name: str) -> str:
    s = name.strip()
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", "_", s)
    return s or "chapter"


def _run(cmd: list[str]) -> None:
    print("\n$ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="run_per_subdir",
        description="按子目录逐个运行 mm-qbank（避免题号冲突）",
    )
    ap.add_argument("--root", type=Path, required=True, help="总目录：包含多个子目录（如 第一章/第二章/...）")
    ap.add_argument(
        "--out-root",
        type=Path,
        default=None,
        help="输出根目录（默认：data/out/batch）",
    )
    ap.add_argument(
        "--config",
        type=Path,
        default=None,
        help="传给 mm-qbank 的 --config（缺省用项目默认 configs/default.yaml）",
    )
    ap.add_argument(
        "--only",
        type=str,
        default=None,
        help="只跑匹配的子目录名（正则），例如 '第[一二三]章' 或 'chapter_\\d+'",
    )
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="若该章已生成 refined_merged.xlsx 则跳过",
    )
    ap.add_argument(
        "--vlm-model",
        type=str,
        default=None,
        help="覆盖 VLM 模型（等价 mm-qbank vlm-text --model ...）",
    )
    ap.add_argument(
        "--llm-model",
        type=str,
        default=None,
        help="覆盖 refine 的 LLM 模型（等价 mm-qbank vlm-refine --model ...）",
    )

    args = ap.parse_args()

    root = args.root.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"--root 不是有效目录: {root}")

    repo_root = Path(__file__).resolve().parents[1]
    out_root = (args.out_root or (repo_root / "data" / "out" / "batch")).resolve()

    only_re = re.compile(args.only) if args.only else None

    subdirs = [p for p in sorted(root.iterdir()) if p.is_dir()]
    subdirs = [p for p in subdirs if (only_re is None or only_re.search(p.name))]
    subdirs = [p for p in subdirs if _has_any_image(p)]

    if not subdirs:
        raise SystemExit(f"未找到可跑的子目录（需要至少包含一张图片）: {root}")

    print(f"root     = {root}")
    print(f"out_root = {out_root}")
    print(f"chapters = {len(subdirs)}")

    for i, sub in enumerate(subdirs, start=1):
        chapter = _safe_name(sub.name)
        chapter_out = out_root / chapter
        vlm_out = chapter_out / "vlm_text"
        compose_out = chapter_out / "llm_compose"
        refine_out = chapter_out / "refine"

        pages_manifest = vlm_out / "pages" / "pages.jsonl"
        compose_jsonl = compose_out / "llm_compose_merged.jsonl"
        refined_xlsx = refine_out / "refined_merged.xlsx"

        print(f"\n===== [{i}/{len(subdirs)}] {sub.name} =====")

        if args.skip_existing and refined_xlsx.exists():
            print(f"skip: 已存在 {refined_xlsx}")
            continue

        vlm_cmd = [
            sys.executable,
            "-m",
            "mm_qbank.cli",
            "vlm-text",
            "--in",
            str(sub),
            "--out-dir",
            str(vlm_out),
        ]
        if args.config:
            vlm_cmd += ["--config", str(args.config)]
        if args.vlm_model:
            vlm_cmd += ["--model", args.vlm_model]
        _run(vlm_cmd)

        llm_cmd = [
            sys.executable,
            "-m",
            "mm_qbank.cli",
            "llm-compose",
            "--manifest",
            str(pages_manifest),
            "--out",
            str(compose_jsonl),
        ]
        if args.config:
            llm_cmd += ["--config", str(args.config)]
        _run(llm_cmd)

        refine_cmd = [
            sys.executable,
            "-m",
            "mm_qbank.cli",
            "vlm-refine",
            "--compose-jsonl",
            str(compose_jsonl),
            "--out-xlsx",
            str(refined_xlsx),
            "--out-jsonl",
            str(refine_out / "refined_merged.jsonl"),
            "--out-csv",
            str(refine_out / "refined_merged_stream.csv"),
        ]
        if args.config:
            refine_cmd += ["--config", str(args.config)]
        if args.llm_model:
            refine_cmd += ["--model", args.llm_model]
        _run(refine_cmd)

    print("\n全部子目录已处理完成。")


if __name__ == "__main__":
    main()
