from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
from sentence_transformers import SentenceTransformer

from mm_qbank.config import project_root


@dataclass(frozen=True)
class Embedder:
    model_name: str

    def embed_texts(self, texts: list[str], *, batch_size: int = 32) -> np.ndarray:
        m = _get_model(self.model_name)
        arr = m.encode(
            texts,
            batch_size=int(batch_size),
            show_progress_bar=False,
            normalize_embeddings=True,
        )
        out = np.asarray(arr, dtype=np.float32)
        if out.ndim != 2:
            raise ValueError("embedding 输出维度异常")
        return out


@lru_cache(maxsize=4)
def _get_model(name: str) -> SentenceTransformer:
    # sentence-transformers 会自动缓存到用户目录；离线使用时需要先下载模型文件。
    # 本项目约定：若项目根下存在 models/<repo_id> 目录，则优先走本地目录，避免联网。
    s = (name or "").strip()
    if not s:
        raise ValueError("model_name 不能为空")
    p = Path(s)
    if p.is_dir():
        return SentenceTransformer(str(p.resolve()))
    # 形如 BAAI/bge-small-zh-v1.5 → <project_root>/models/BAAI/bge-small-zh-v1.5
    if "/" in s or "\\" in s:
        local = project_root() / "models" / Path(s)
        if local.is_dir():
            return SentenceTransformer(str(local.resolve()))
    return SentenceTransformer(s)


def embed_iter(
    embedder: Embedder,
    texts: Iterable[str],
    *,
    batch_size: int = 32,
    total: int | None = None,
    on_batch: Callable[[int, int | None], None] | None = None,
) -> np.ndarray:
    buf: list[str] = []
    out_parts: list[np.ndarray] = []
    done = 0
    for t in texts:
        buf.append(t)
        if len(buf) >= batch_size:
            out_parts.append(embedder.embed_texts(buf, batch_size=batch_size))
            done += len(buf)
            if on_batch is not None:
                on_batch(done, total)
            buf.clear()
    if buf:
        out_parts.append(embedder.embed_texts(buf, batch_size=batch_size))
        done += len(buf)
        if on_batch is not None:
            on_batch(done, total)
    if not out_parts:
        return np.zeros((0, 0), dtype=np.float32)
    return np.vstack(out_parts)

