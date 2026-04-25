from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def vlm_classified_items_to_plaintext(items: list[dict[str, Any]]) -> str:
    """将 VLM 分类后的 ``items`` 拼成可给 ``llm-compose`` 用的整页纯文本（带题号/类型标签）。"""
    parts: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        num = str(it.get("题号", "")).strip()
        typ = str(it.get("类型", "")).strip()
        content = (it.get("内容") or it.get("content") or "")
        if isinstance(content, str):
            content = content.replace("\r\n", "\n").strip()
        else:
            content = str(content).strip()
        if not typ:
            continue
        head = f"【{typ} {num}】" if num else f"【{typ}】"
        if content:
            parts.append(f"{head}\n{content}")
        else:
            parts.append(head)
    return "\n\n".join(parts)


def export_vlm_classified_artifact(
    *,
    work: Path,
    page_id: str,
    source_image: Path,
    items: list[dict[str, Any]],
    subdir: str,
    extra: dict[str, Any] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """
    写入 ``{page_id}.json``（含分类 segments）与 ``{page_id}.txt``（合并纯文本），
    返回 (json 路径, txt 路径, manifest 行 dict)，行中含 ``structured_file`` 与 ``text_file``。
    """
    base = work / subdir
    base.mkdir(parents=True, exist_ok=True)
    body: dict[str, Any] = {
        "page_id": page_id,
        "items": items,
    }
    if extra:
        body.update(extra)
    json_path = (base / f"{page_id}.json").resolve()
    json_path.write_text(
        json.dumps(body, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    plain = vlm_classified_items_to_plaintext(items)
    txt_path = (base / f"{page_id}.txt").resolve()
    txt_path.write_text(plain, encoding="utf-8")
    w = work.resolve()
    rel_j = json_path.relative_to(w)
    rel_t = txt_path.relative_to(w)
    n_lines = len(plain.splitlines()) if plain else 0
    row: dict[str, Any] = {
        "page_id": page_id,
        "source_image": str(source_image.resolve()),
        "text_file": str(rel_t).replace("\\", "/"),
        "structured_file": str(rel_j).replace("\\", "/"),
        "line_count": n_lines,
        "char_count": len(plain),
        "segment_count": len(items),
        "recognition": "vlm",
    }
    return json_path, txt_path, row


def write_page_text_manifest(work: Path, subdir: str, manifest_name: str, rows: list[dict[str, Any]]) -> Path | None:
    if not rows:
        return None
    path = (work / subdir / manifest_name).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    path.write_text(body, encoding="utf-8")
    return path
