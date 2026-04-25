from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from mm_qbank.kb.pdf_extract import PDFPageText


@dataclass(frozen=True)
class KBChunk:
    id: str
    pdf_name: str
    page_index: int
    text: str


def chunk_pages(
    pages: Iterable[PDFPageText],
    *,
    chunk_chars: int = 900,
    overlap: int = 120,
) -> list[KBChunk]:
    chunk_chars = int(chunk_chars)
    overlap = int(overlap)
    if chunk_chars < 200:
        chunk_chars = 200
    if overlap < 0:
        overlap = 0
    if overlap >= chunk_chars:
        overlap = max(0, chunk_chars // 4)

    out: list[KBChunk] = []
    for pg in pages:
        t = (pg.text or "").strip()
        if not t:
            continue
        st = 0
        idx = 0
        while st < len(t):
            ed = min(len(t), st + chunk_chars)
            body = t[st:ed].strip()
            if body:
                cid = f"{pg.pdf_name}::p{pg.page_index+1}::c{idx}"
                out.append(KBChunk(id=cid, pdf_name=pg.pdf_name, page_index=pg.page_index, text=body))
                idx += 1
            if ed >= len(t):
                break
            st = max(0, ed - overlap)
    return out

