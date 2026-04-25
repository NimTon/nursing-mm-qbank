from __future__ import annotations

import re

from mm_qbank.kb.index_faiss import KBSearchHit


_WS = re.compile(r"\s+")


def _one_line(s: str) -> str:
    return _WS.sub(" ", (s or "").strip())


def format_refs(hits: list[KBSearchHit], *, max_chars_per_ref: int = 380) -> str:
    if not hits:
        return ""
    parts: list[str] = []
    parts.append("【优先参考资料（离线PDF知识库TopK）】")
    for i, h in enumerate(hits, start=1):
        txt = _one_line(h.chunk.text)
        if max_chars_per_ref > 0 and len(txt) > max_chars_per_ref:
            txt = txt[: max_chars_per_ref - 1] + "…"
        parts.append(
            f"[{i}] ({h.chunk.pdf_name} p.{h.chunk.page_index+1} score={h.score:.3f}) {txt}"
        )
    parts.append(
        "要求：修正/补充时必须优先依据以上参考资料；资料不足请明确写“资料不足”；不得编造参考来源。"
    )
    return "\n".join(parts).strip()

