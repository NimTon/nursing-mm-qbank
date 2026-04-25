from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from mm_qbank.kb.chunking import chunk_pages
from mm_qbank.kb.embed import Embedder
from mm_qbank.kb.pdf_extract import iter_pdf_pages, list_pdfs
from mm_qbank.kb.kb_store import save_kb


def build_kb_from_pdf_dir(
    *,
    pdf_dir: Path,
    kb_root: Path,
    model_name: str,
    chunk_chars: int = 900,
    overlap: int = 120,
    embed_batch_size: int = 32,
) -> dict[str, object]:
    pdf_dir = pdf_dir.resolve()
    kb_root = kb_root.resolve()
    pdfs = list_pdfs(pdf_dir)
    pages = []
    for p in pdfs:
        pages.extend(iter_pdf_pages(p))
    chunks = chunk_pages(pages, chunk_chars=chunk_chars, overlap=overlap)

    embedder = Embedder(model_name)
    t0 = time.time()
    embs = embedder.embed_texts([c.text for c in chunks], batch_size=embed_batch_size)
    t1 = time.time()

    manifest = {
        "version": 1,
        "pdf_dir": str(pdf_dir),
        "kb_root": str(kb_root),
        "model_name": model_name,
        "chunk_chars": int(chunk_chars),
        "overlap": int(overlap),
        "n_pdfs": len(pdfs),
        "n_pages": len(pages),
        "n_chunks": len(chunks),
        "built_at_unix": int(time.time()),
        "embed_seconds": float(t1 - t0),
    }
    save_kb(kb_root=kb_root, chunks=chunks, embeddings=embs, manifest=manifest)
    return {
        "kb_root": str(kb_root),
        "manifest": manifest,
        "n_pdfs": len(pdfs),
        "n_pages": len(pages),
        "n_chunks": len(chunks),
    }

