from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

try:
    import pypdf  # noqa: F401
    import faiss  # noqa: F401
except Exception as e:  # noqa: BLE001
    pytest.fail(
        "KB/RAG dependencies are missing or unusable.\n"
        "Run in project root:\n"
        "  pip install -e .\n"
        "or at least:\n"
        "  pip install pypdf faiss-cpu sentence-transformers\n"
        f"\nOriginal error: {e}\n"
    )

from mm_qbank.kb.chunking import KBChunk
from mm_qbank.kb.index_faiss import (
    build_faiss_index,
    load_chunks_jsonl,
    load_faiss_index,
    load_kb_manifest,
    save_chunks_jsonl,
    save_faiss_index,
    save_kb_manifest,
    search,
)


def _l2_normalize(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(x, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return x / n


def test_kb_faiss_roundtrip_and_search(tmp_path: Path) -> None:
    # 这个测试刻意不触发 sentence-transformers（避免联网下载模型）
    chunks = [
        KBChunk(id="a::p1::c0", pdf_name="a.pdf", page_index=0, text="护理评估包括主观资料与客观资料。"),
        KBChunk(id="a::p1::c1", pdf_name="a.pdf", page_index=0, text="血压测量需要选择合适袖带，保持手臂与心脏同高。"),
        KBChunk(id="b::p2::c0", pdf_name="b.pdf", page_index=1, text="无菌技术的核心是防止污染与交叉感染。"),
    ]

    rng = np.random.default_rng(0)
    embs = _l2_normalize(rng.normal(size=(len(chunks), 16)).astype(np.float32))
    index = build_faiss_index(embs)

    kb_root = tmp_path / "kb_demo"
    kb_root.mkdir(parents=True, exist_ok=True)
    index_path = kb_root / "index.faiss"
    chunks_path = kb_root / "chunks.jsonl"
    manifest_path = kb_root / "kb_manifest.json"

    save_faiss_index(index_path, index)
    save_chunks_jsonl(chunks_path, chunks)
    save_kb_manifest(
        manifest_path,
        {
            "version": 1,
            "model_name": "DUMMY/NO-DOWNLOAD",
            "chunk_chars": 900,
            "overlap": 120,
            "n_chunks": len(chunks),
        },
    )

    index2 = load_faiss_index(index_path)
    chunks2 = load_chunks_jsonl(chunks_path)
    manifest2 = load_kb_manifest(manifest_path)

    assert manifest2["n_chunks"] == 3
    assert [c.id for c in chunks2] == [c.id for c in chunks]

    # 用第一条 embedding 当 query，理论上应命中自己为 top1
    hits = search(index=index2, chunks=chunks2, query_embedding=embs[0], topk=3)
    assert len(hits) >= 1
    assert hits[0].chunk.id == chunks[0].id
    assert hits[0].score >= hits[-1].score

