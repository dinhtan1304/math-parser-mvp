from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def extract_native_pdf_markdown(path: str | Path, *, min_chars: int = 80) -> dict[str, Any]:
    """Fast text-layer extraction for PDFs that are not scans.

    This intentionally avoids Marker/MinerU layout and formula models. It is the
    first pass for text-based PDFs; callers can still escalate to OCR when this
    output is empty or fails quality checks.
    """
    pdf_path = Path(path)
    if pdf_path.suffix.lower() != ".pdf":
        return {
            "text": "",
            "image_map": {},
            "method": "native-pdf-unsupported",
            "page_count": 0,
            "requires_latex_normalization": False,
        }
    try:
        import fitz  # type: ignore

        doc = fitz.open(pdf_path)
        try:
            pages = []
            for index, page in enumerate(doc, start=1):
                text = _normalize_page_text(page.get_text("text") or "")
                if text:
                    pages.append(f"<!-- page:{index} -->\n{text}")
            markdown = "\n\n".join(pages).strip()
            page_count = len(doc)
        finally:
            doc.close()
    except Exception as exc:
        return {
            "text": "",
            "image_map": {},
            "method": "native-pdf-error",
            "page_count": 0,
            "error": str(exc),
            "requires_latex_normalization": False,
        }

    method = "native-pdf-text" if len(markdown.strip()) >= min_chars else "native-pdf-too-short"
    return {
        "text": markdown,
        "image_map": {},
        "method": method,
        "page_count": page_count,
        "requires_latex_normalization": bool(markdown.strip()),
    }


def _normalize_page_text(text: str) -> str:
    lines = []
    for raw_line in (text or "").splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)
