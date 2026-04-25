from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass(frozen=True)
class PDFPageText:
    pdf_path: Path
    pdf_name: str
    page_index: int  # 0-based
    text: str


_WS_RE = re.compile(r"[ \t\u3000]+")
_MANY_NL_RE = re.compile(r"\n{3,}")


def _clean_text(s: str) -> str:
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = _WS_RE.sub(" ", s)
    s = _MANY_NL_RE.sub("\n\n", s)
    return s.strip()


def iter_pdf_pages(pdf_path: Path) -> list[PDFPageText]:
    pdf_path = pdf_path.resolve()
    r = PdfReader(str(pdf_path))
    out: list[PDFPageText] = []
    for i, p in enumerate(r.pages):
        try:
            t = p.extract_text() or ""
        except Exception:  # noqa: BLE001
            t = ""
        t = _clean_text(t)
        if not t:
            continue
        out.append(PDFPageText(pdf_path=pdf_path, pdf_name=pdf_path.name, page_index=i, text=t))
    return out


def list_pdfs(pdf_dir: Path) -> list[Path]:
    pdf_dir = pdf_dir.resolve()
    return sorted([p for p in pdf_dir.rglob("*.pdf") if p.is_file()])

