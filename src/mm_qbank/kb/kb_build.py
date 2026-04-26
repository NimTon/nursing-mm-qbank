from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from mm_qbank.kb.chunking import chunk_pages
from mm_qbank.kb.embed import Embedder, embed_iter
from mm_qbank.kb.pdf_extract import iter_pdf_pages, list_pdfs
from mm_qbank.kb.kb_store import save_kb


def build_kb_from_pdf_paths(
    *,
    pdf_paths: list[Path],
    kb_root: Path,
    model_name: str,
    kb_display_name: str | None = None,
    chunk_chars: int = 900,
    overlap: int = 120,
    embed_batch_size: int = 32,
    on_progress: Callable[[str, int, int | None, str], None] | None = None,
) -> dict[str, object]:
    """从明确的 PDF 列表构建 KB（多 PDF 会合并为一个 KB）。"""
    kb_root = kb_root.resolve()
    pdfs = [p.resolve() for p in pdf_paths if p and Path(p).is_file()]
    if not pdfs:
        raise ValueError("pdf_paths 为空或均不可用")

    if on_progress is not None:
        on_progress("list_pdfs", len(pdfs), len(pdfs), f"发现 PDF: {len(pdfs)} 个")

    pages = []
    for i, p in enumerate(pdfs, start=1):
        if on_progress is not None:
            on_progress("extract_pdf", i, len(pdfs), f"提取页面: {p.name}（{i}/{len(pdfs)}）")
        pages.extend(iter_pdf_pages(p))

    if on_progress is not None:
        on_progress("extract_pages_done", len(pages), None, f"已提取页面: {len(pages)} 页")

    chunks = chunk_pages(pages, chunk_chars=chunk_chars, overlap=overlap)
    if on_progress is not None:
        on_progress("chunk_done", len(chunks), None, f"切块完成: {len(chunks)} 段")

    embedder = Embedder(model_name)
    t0 = time.time()
    if on_progress is not None:
        on_progress("embed_start", 0, len(chunks), f"开始 embedding：共 {len(chunks)} 段，batch={int(embed_batch_size)}")

    def _on_batch(done: int, total: int | None) -> None:
        if on_progress is not None:
            on_progress("embed_batch", int(done), total, f"embedding 进度: {done}/{total if total is not None else '?'}")

    embs = embed_iter(
        embedder,
        (c.text for c in chunks),
        batch_size=embed_batch_size,
        total=len(chunks),
        on_batch=_on_batch,
    )
    t1 = time.time()
    if on_progress is not None:
        on_progress("embed_done", len(chunks), len(chunks), f"embedding 完成，用时 {t1 - t0:.2f}s")

    manifest = {
        "version": 1,
        "pdf_dir": None,
        "pdf_paths": [str(p) for p in pdfs],
        "kb_root": str(kb_root),
        "kb_display_name": (str(kb_display_name).strip() if kb_display_name else None),
        "model_name": model_name,
        "chunk_chars": int(chunk_chars),
        "overlap": int(overlap),
        "n_pdfs": len(pdfs),
        "n_pages": len(pages),
        "n_chunks": len(chunks),
        "built_at_unix": int(time.time()),
        "embed_seconds": float(t1 - t0),
    }
    if on_progress is not None:
        on_progress("save_start", 0, None, "保存 KB（FAISS/manifest/chunks）…")
    save_kb(kb_root=kb_root, chunks=chunks, embeddings=embs, manifest=manifest)
    if on_progress is not None:
        on_progress("save_done", 1, 1, "保存完成")

    return {
        "kb_root": str(kb_root),
        "manifest": manifest,
        "n_pdfs": len(pdfs),
        "n_pages": len(pages),
        "n_chunks": len(chunks),
    }


def build_kb_from_pdf_dir(
    *,
    pdf_dir: Path,
    kb_root: Path,
    model_name: str,
    chunk_chars: int = 900,
    overlap: int = 120,
    embed_batch_size: int = 32,
    on_progress: Callable[[str, int, int | None, str], None] | None = None,
) -> dict[str, object]:
    pdf_dir = pdf_dir.resolve()
    pdfs = list_pdfs(pdf_dir)
    out = build_kb_from_pdf_paths(
        pdf_paths=pdfs,
        kb_root=kb_root,
        model_name=model_name,
        chunk_chars=chunk_chars,
        overlap=overlap,
        embed_batch_size=embed_batch_size,
        on_progress=on_progress,
    )
    # 保持旧字段：pdf_dir
    try:
        mf = out.get("manifest")
        if isinstance(mf, dict):
            mf["pdf_dir"] = str(pdf_dir)
    except Exception:
        pass
    return out

