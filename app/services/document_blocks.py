from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_RE_PAGE_MARKER = re.compile(r"^\s*\[Trang\s+(\d+)\]\s*$", re.IGNORECASE)
_RE_MARKDOWN_HEADING = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
_RE_QUESTION = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:Câu|Bài|Question)\s+(\d+)(?=\D|$)",
    re.IGNORECASE,
)
_RE_NUMBERED = re.compile(r"^\s*(\d+)\s*[.)]\s+\S+")
_RE_SUBQUESTION = re.compile(r"^\s*[a-zA-Z]\s*[.)]\s+\S+")
_RE_ANSWER_HEADER = re.compile(
    r"(?:hướng\s+dẫn\s+ch(?:ấ|ẩ)m|đáp\s+án|lời\s+giải|bài\s+giải|answer\s+key)",
    re.IGNORECASE,
)
_RE_ANSWER_SECTION_HEADING = re.compile(
    r"^\s*(?:"
    r"hướng\s+dẫn\s+ch(?:ấ|ẩ)m"
    r"|đáp\s+án(?:\s+(?:chi\s+tiết|và\s+thang\s+điểm))?"
    r"|lời\s+giải"
    r"|bài\s+giải(?:\s+tham\s+khảo)?"
    r"|answer\s+key"
    r")\s*[:.]?\s*$",
    re.IGNORECASE,
)
_RE_BOILERPLATE = re.compile(
    r"(?:thí\s+sinh\s+không\s+được|họ\s+và\s+tên\s+thí\s+sinh|số\s+báo\s+danh|"
    r"^\s*[—\-=_\s]*hết[—\-=_\s]*$)",
    re.IGNORECASE | re.MULTILINE,
)
_RE_FOOTER = re.compile(r"^\s*\d+\s*/\s*\d+\s*$")
_RE_EDGE_EMPHASIS = re.compile(r"^\s*(?:\*{1,2}|_{1,2})\s*(.*?)\s*(?:\*{1,2}|_{1,2})\s*$")


@dataclass
class DocumentBlock:
    block_id: str
    page_num: int
    order: int
    text: str
    source: str
    bbox: tuple[float, float, float, float] | None = None
    kind: str = "unknown"
    markdown_level: int | None = None
    features: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentPage:
    page_num: int
    blocks: list[DocumentBlock] = field(default_factory=list)


def build_document_pages(ocr_result: dict[str, Any]) -> list[DocumentPage]:
    """Normalize OCR output into page/block objects.

    Existing OCR backends mostly expose flat text today, so v1 uses a robust
    paragraph fallback while preserving page markers when they exist.  If a
    backend later adds native blocks, this shape can absorb them without changing
    the segmentation API.
    """
    text = str(ocr_result.get("text") or "")
    source = str(ocr_result.get("method") or "unknown")
    raw_blocks = ocr_result.get("blocks")
    if isinstance(raw_blocks, list) and raw_blocks:
        return _pages_from_native_blocks(raw_blocks, source)
    return _pages_from_text(text, source)


def _pages_from_native_blocks(raw_blocks: list[dict[str, Any]], source: str) -> list[DocumentPage]:
    pages: dict[int, DocumentPage] = {}
    for order, raw in enumerate(raw_blocks):
        page_num = int(raw.get("page_num") or 1)
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        page = pages.setdefault(page_num, DocumentPage(page_num=page_num))
        block = DocumentBlock(
            block_id=str(raw.get("block_id") or f"p{page_num}_b{len(page.blocks) + 1}"),
            page_num=page_num,
            order=int(raw.get("order", order)),
            text=text,
            source=str(raw.get("source") or source),
            bbox=tuple(raw["bbox"]) if raw.get("bbox") else None,
            kind=str(raw.get("kind") or "unknown"),
            markdown_level=raw.get("markdown_level"),
        )
        block.features = _build_features(block)
        page.blocks.append(block)
    return [pages[k] for k in sorted(pages)]


def _pages_from_text(text: str, source: str) -> list[DocumentPage]:
    page_num = 1
    pages: dict[int, DocumentPage] = {1: DocumentPage(page_num=1)}
    buffer: list[str] = []
    order = 0

    def flush() -> None:
        nonlocal order
        chunk = "\n".join(buffer).strip()
        buffer.clear()
        if not chunk:
            return
        page = pages.setdefault(page_num, DocumentPage(page_num=page_num))
        block = _make_block(page_num, len(page.blocks) + 1, order, chunk, source)
        page.blocks.append(block)
        order += 1

    for line in text.splitlines():
        marker = _RE_PAGE_MARKER.match(line)
        if marker:
            flush()
            page_num = int(marker.group(1))
            pages.setdefault(page_num, DocumentPage(page_num=page_num))
            continue
        if not line.strip():
            flush()
            continue
        heading = _RE_MARKDOWN_HEADING.match(line)
        if heading and buffer:
            flush()
        buffer.append(line)
        if heading:
            flush()
    flush()
    return [pages[k] for k in sorted(pages)]


def _make_block(page_num: int, ordinal: int, order: int, text: str, source: str) -> DocumentBlock:
    markdown_level = None
    kind = "paragraph"
    heading = _RE_MARKDOWN_HEADING.match(text)
    if heading:
        markdown_level = len(heading.group(1))
        kind = "heading"
    elif text.lstrip().startswith("|") and text.rstrip().endswith("|"):
        kind = "table"
    elif text.lstrip().startswith("![") or "[HÌNH_" in text.upper():
        kind = "figure"
    elif "$" in text or "\\" in text:
        kind = "formula" if len(text.split()) <= 20 else "paragraph"

    block = DocumentBlock(
        block_id=f"p{page_num}_b{ordinal}",
        page_num=page_num,
        order=order,
        text=text,
        source=source,
        kind=kind,
        markdown_level=markdown_level,
    )
    block.features = _build_features(block)
    return block


def _build_features(block: DocumentBlock) -> dict[str, Any]:
    text = block.text.strip()
    lower = text.lower()
    heading_match = _RE_MARKDOWN_HEADING.match(text)
    normalized = heading_match.group(2).strip() if heading_match else text
    emphasis = _RE_EDGE_EMPHASIS.match(normalized)
    if emphasis:
        normalized = emphasis.group(1).strip()
    question_match = _RE_QUESTION.match(text) or _RE_QUESTION.match(normalized)
    numbered_match = _RE_NUMBERED.match(normalized)
    return {
        "normalized_text": normalized,
        "char_count": len(text),
        "line_count": len(text.splitlines()),
        "is_heading": bool(heading_match),
        "has_question_marker": bool(question_match),
        "question_num": int(question_match.group(1)) if question_match else (
            int(numbered_match.group(1)) if numbered_match else None
        ),
        "has_numbered_marker": bool(numbered_match),
        "has_subquestion_marker": bool(_RE_SUBQUESTION.match(normalized)),
        "has_answer_header": bool(_RE_ANSWER_HEADER.search(normalized)),
        "is_answer_section_heading": bool(_RE_ANSWER_SECTION_HEADING.match(normalized)),
        "is_boilerplate": bool(_RE_BOILERPLATE.search(normalized)),
        "is_footer": bool(_RE_FOOTER.match(normalized)),
        "looks_like_table": block.kind == "table",
        "looks_like_figure": block.kind == "figure",
        "contains_score_note": bool(re.search(r"\(\s*\d+(?:[.,]\d+)?\s*điểm\s*\)", lower)),
    }
