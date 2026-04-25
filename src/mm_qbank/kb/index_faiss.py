from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import faiss
import numpy as np

from mm_qbank.kb.chunking import KBChunk


@dataclass(frozen=True)
class KBSearchHit:
    chunk: KBChunk
    score: float


def save_chunks_jsonl(path: Path, chunks: list[KBChunk]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(
                json.dumps(
                    {
                        "id": c.id,
                        "pdf_name": c.pdf_name,
                        "page_index": c.page_index,
                        "text": c.text,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def load_chunks_jsonl(path: Path) -> list[KBChunk]:
    out: list[KBChunk] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if not isinstance(d, dict):
            continue
        out.append(
            KBChunk(
                id=str(d.get("id", "")),
                pdf_name=str(d.get("pdf_name", "")),
                page_index=int(d.get("page_index", 0) or 0),
                text=str(d.get("text", "")),
            )
        )
    return out


def build_faiss_index(embeddings: np.ndarray) -> faiss.Index:
    if embeddings.dtype != np.float32:
        embeddings = embeddings.astype(np.float32)
    if embeddings.ndim != 2:
        raise ValueError("embeddings 必须是二维矩阵")
    d = int(embeddings.shape[1])
    # embeddings 已 normalize_embeddings=True，可用 inner product 近似 cosine
    index = faiss.IndexFlatIP(d)
    index.add(embeddings)
    return index


def save_faiss_index(path: Path, index: faiss.Index) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(path))


def load_faiss_index(path: Path) -> faiss.Index:
    return faiss.read_index(str(path.resolve()))


def search(
    *,
    index: faiss.Index,
    chunks: list[KBChunk],
    query_embedding: np.ndarray,
    topk: int = 5,
) -> list[KBSearchHit]:
    topk = max(1, int(topk))
    q = query_embedding.astype(np.float32)
    if q.ndim == 1:
        q = q.reshape(1, -1)
    if q.ndim != 2:
        raise ValueError("query_embedding 维度异常")
    scores, ids = index.search(q, topk)
    out: list[KBSearchHit] = []
    for j in range(min(topk, ids.shape[1])):
        idx = int(ids[0, j])
        if idx < 0 or idx >= len(chunks):
            continue
        out.append(KBSearchHit(chunk=chunks[idx], score=float(scores[0, j])))
    return out


def save_kb_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_kb_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))

