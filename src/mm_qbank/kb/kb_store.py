from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mm_qbank.config import project_root
from mm_qbank.kb.chunking import KBChunk
from mm_qbank.kb.embed import Embedder
from mm_qbank.kb.index_faiss import (
    KBSearchHit,
    build_faiss_index,
    load_chunks_jsonl,
    load_faiss_index,
    load_kb_manifest,
    save_chunks_jsonl,
    save_faiss_index,
    save_kb_manifest,
    search,
)


@dataclass(frozen=True)
class KBStorePaths:
    root: Path
    manifest: Path
    chunks: Path
    index: Path


def kb_dir_from_arg(kb: str | Path) -> Path:
    """支持传 name（落在 data/kb/name）或传绝对/相对目录路径。"""
    if isinstance(kb, Path):
        p = kb
    else:
        s = str(kb).strip()
        p = Path(s)
    if str(p).strip() and (p.exists() or p.parent.exists() or p.is_absolute() or any(sep in str(p) for sep in ("/", "\\"))):
        # 传的是路径（或看起来像路径）
        return p.resolve()
    return (project_root() / "data" / "kb" / str(kb)).resolve()


def kb_paths(kb_root: Path) -> KBStorePaths:
    kb_root = kb_root.resolve()
    return KBStorePaths(
        root=kb_root,
        manifest=kb_root / "kb_manifest.json",
        chunks=kb_root / "chunks.jsonl",
        index=kb_root / "index.faiss",
    )


def save_kb(
    *,
    kb_root: Path,
    chunks: list[KBChunk],
    embeddings: Any,
    manifest: dict[str, Any],
) -> KBStorePaths:
    p = kb_paths(kb_root)
    p.root.mkdir(parents=True, exist_ok=True)
    idx = build_faiss_index(embeddings)
    save_faiss_index(p.index, idx)
    save_chunks_jsonl(p.chunks, chunks)
    save_kb_manifest(p.manifest, manifest)
    return p


@dataclass
class KBStore:
    root: Path
    embedder: Embedder
    chunks: list[KBChunk]
    index: Any
    manifest: dict[str, Any]

    def query(self, text: str, *, topk: int = 5) -> list[KBSearchHit]:
        emb = self.embedder.embed_texts([text], batch_size=1)[0]
        return search(index=self.index, chunks=self.chunks, query_embedding=emb, topk=topk)


def load_kb(*, kb_root: Path, model_name: str | None = None) -> KBStore:
    p = kb_paths(kb_root)
    if not p.manifest.is_file() or not p.index.is_file() or not p.chunks.is_file():
        raise FileNotFoundError(f"知识库目录不完整: {p.root}")
    manifest = load_kb_manifest(p.manifest)
    m = (model_name or "").strip() or str(manifest.get("model_name") or "")
    if not m:
        raise ValueError("知识库 manifest 缺少 model_name")
    embedder = Embedder(m)
    chunks = load_chunks_jsonl(p.chunks)
    index = load_faiss_index(p.index)
    return KBStore(root=p.root, embedder=embedder, chunks=chunks, index=index, manifest=manifest)

