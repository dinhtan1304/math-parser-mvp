from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal


PdfKind = Literal["text_pdf", "scan_pdf", "mixed_pdf", "unknown"]
OcrMode = Literal["text", "ocr", "mixed", "auto"]


@dataclass
class PdfPageSignal:
    page_num: int
    text_chars: int = 0
    text_blocks: int = 0
    image_count: int = 0
    image_coverage: float = 0.0
    kind: str = "unknown"


@dataclass
class PdfOcrAnalysis:
    page_count: int = 0
    has_text_layer: bool = False
    pdf_kind: PdfKind = "unknown"
    recommended_ocr_mode: OcrMode = "auto"
    text_page_ratio: float = 0.0
    scan_page_ratio: float = 0.0
    avg_text_chars: float = 0.0
    pages: list[PdfPageSignal] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def analyze_pdf_for_ocr(path: str | Path, *, max_pages: int = 12) -> PdfOcrAnalysis:
    pdf_path = Path(path)
    if pdf_path.suffix.lower() != ".pdf":
        return PdfOcrAnalysis()
    try:
        import fitz  # type: ignore

        doc = fitz.open(pdf_path)
        try:
            page_count = len(doc)
            if page_count <= 0:
                return PdfOcrAnalysis(page_count=0)
            sample_count = min(page_count, max(1, max_pages))
            pages = [_page_signal(doc[i], i + 1) for i in range(sample_count)]
        finally:
            doc.close()
    except Exception:
        return PdfOcrAnalysis()

    text_pages = sum(1 for page in pages if page.kind == "text")
    scan_pages = sum(1 for page in pages if page.kind == "scan")
    sample_count = max(1, len(pages))
    text_ratio = text_pages / sample_count
    scan_ratio = scan_pages / sample_count
    avg_text_chars = sum(page.text_chars for page in pages) / sample_count

    if text_ratio >= 0.65 and scan_ratio < 0.25:
        pdf_kind: PdfKind = "text_pdf"
        mode: OcrMode = "text"
    elif scan_ratio >= 0.65 and text_ratio < 0.25:
        pdf_kind = "scan_pdf"
        mode = "ocr"
    elif text_pages and scan_pages:
        pdf_kind = "mixed_pdf"
        mode = "mixed"
    elif avg_text_chars >= 80:
        pdf_kind = "text_pdf"
        mode = "text"
    elif scan_pages:
        pdf_kind = "scan_pdf"
        mode = "ocr"
    else:
        pdf_kind = "unknown"
        mode = "auto"

    return PdfOcrAnalysis(
        page_count=page_count,
        has_text_layer=text_pages > 0 or avg_text_chars >= 50,
        pdf_kind=pdf_kind,
        recommended_ocr_mode=mode,
        text_page_ratio=round(text_ratio, 3),
        scan_page_ratio=round(scan_ratio, 3),
        avg_text_chars=round(avg_text_chars, 1),
        pages=pages,
    )


def recommended_ocr_mode(path: str | Path) -> OcrMode:
    return analyze_pdf_for_ocr(path).recommended_ocr_mode


def _page_signal(page, page_num: int) -> PdfPageSignal:
    text = page.get_text("text") or ""
    text_chars = len("".join(text.split()))
    text_blocks = 0
    image_count = 0
    image_area = 0.0
    page_area = max(float(page.rect.width * page.rect.height), 1.0)

    try:
        for block in page.get_text("dict").get("blocks", []):
            block_type = block.get("type")
            if block_type == 0:
                block_text = _block_text(block)
                if block_text.strip():
                    text_blocks += 1
            elif block_type == 1:
                image_count += 1
                image_area += _bbox_area(block.get("bbox"))
    except Exception:
        pass

    try:
        image_count = max(image_count, len(page.get_images(full=True)))
    except Exception:
        pass

    image_coverage = min(1.0, image_area / page_area)
    if text_chars >= 80 or (text_chars >= 40 and text_blocks >= 2):
        kind = "text"
    elif text_chars < 30 and (image_coverage >= 0.45 or image_count > 0):
        kind = "scan"
    elif text_chars >= 30:
        kind = "text"
    else:
        kind = "unknown"

    return PdfPageSignal(
        page_num=page_num,
        text_chars=text_chars,
        text_blocks=text_blocks,
        image_count=image_count,
        image_coverage=round(image_coverage, 3),
        kind=kind,
    )


def _block_text(block: dict) -> str:
    parts: list[str] = []
    for line in block.get("lines", []) or []:
        for span in line.get("spans", []) or []:
            parts.append(str(span.get("text") or ""))
    return "".join(parts)


def _bbox_area(bbox) -> float:
    if not bbox or len(bbox) < 4:
        return 0.0
    x0, y0, x1, y1 = [float(v) for v in bbox[:4]]
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)
