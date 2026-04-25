from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_pypdf_read_input_pdf_and_print_text() -> None:
    """
    读取用户指定的 PDF，并把提取到的文本打印到 stdout。

    用法（Windows PowerShell）:
      $env:MM_QBANK_TEST_PDF="D:\\path\\to\\file.pdf"
      pytest -q -s test/test_pypdf_input_pdf.py

    说明:
    - 如果未设置环境变量 MM_QBANK_TEST_PDF，则自动 skip（避免在 CI 里失败）
    - 若 PDF 为扫描版图片，extract_text() 可能为空/None，这是正常现象（需要 OCR 才能出文字）
    """

    pdf_path_raw = os.getenv("MM_QBANK_TEST_PDF", "").strip().strip('"')
    if not pdf_path_raw:
        pytest.skip("未设置环境变量 MM_QBANK_TEST_PDF，跳过输入 PDF 读取测试。")

    pdf_path = Path(pdf_path_raw)
    if not pdf_path.exists() or not pdf_path.is_file():
        pytest.fail(f"MM_QBANK_TEST_PDF 指向的文件不存在: {pdf_path}")

    try:
        from pypdf import PdfReader
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"pypdf 无法导入: {e}")

    reader = PdfReader(str(pdf_path))
    assert len(reader.pages) >= 1

    texts: list[str] = []
    for i, page in enumerate(reader.pages):
        t = page.extract_text() or ""
        if t:
            texts.append(t)

    combined = "\n\n".join(texts).strip()

    # 打印（pytest 默认捕获输出；用 -s 才能直接看到）
    print(f"\n=== PDF: {pdf_path} ===")
    print(f"pages={len(reader.pages)} extracted_chars={len(combined)}")
    if combined:
        print("\n--- extracted_text (first 2000 chars) ---")
        print(combined[:2000])
    else:
        print("\n--- extracted_text is empty (可能是扫描版/无文本层) ---")

