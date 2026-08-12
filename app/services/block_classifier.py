from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.document_blocks import DocumentBlock


_RE_TITLE = re.compile(r"(?:đề\s+thi|phiếu\s+bài\s+tập|worksheet|chuyên\s+đề)", re.IGNORECASE)
_RE_SECTION = re.compile(r"^(?:phần|section)\s+[A-Z0-9IVX]+", re.IGNORECASE)
_RE_INLINE_SOLUTION = re.compile(
    r"(?:lời\s+giải|hướng\s+dẫn\s+giải|giải\s*:|đáp\s+án\s*:|chọn\s+[A-D])",
    re.IGNORECASE,
)
_RE_ANSWER_TABLE = re.compile(r"(?:\b1\s*[.:\-]?\s*[A-D]\b.*\b2\s*[.:\-]?\s*[A-D]\b)")


@dataclass
class BlockClassification:
    block_id: str
    role: str
    confidence: float
    reasons: list[str]


def classify_blocks(blocks: list[DocumentBlock]) -> dict[str, BlockClassification]:
    """Rule-first block classifier used by the v1 segmentation engine."""
    repeated_texts = _repeated_short_texts(blocks)
    out: dict[str, BlockClassification] = {}
    total = max(len(blocks), 1)
    for idx, block in enumerate(blocks):
        out[block.block_id] = _classify_block(
            block,
            index=idx,
            total=total,
            repeated_texts=repeated_texts,
        )
    return out


def _classify_block(
    block: DocumentBlock,
    *,
    index: int,
    total: int,
    repeated_texts: set[str],
) -> BlockClassification:
    f = block.features
    text = f["normalized_text"]
    reasons: list[str] = []

    if f["looks_like_figure"]:
        return BlockClassification(block.block_id, "figure", 0.99, ["figure_placeholder"])
    if f["is_boilerplate"]:
        return BlockClassification(block.block_id, "boilerplate", 0.98, ["boilerplate_pattern"])
    if f["has_answer_header"] and (f["is_heading"] or f["is_answer_section_heading"]):
        return BlockClassification(block.block_id, "answer_key_header", 0.97, ["answer_header_pattern"])
    # Question marker MUST win over the footer/repeated-text heuristic.
    # Vietnamese HSG exams repeat "Câu N (X điểm)" headers in the answer key
    # section, so the same short text appears on multiple pages — the legacy
    # _repeated_short_texts check otherwise mis-classifies legitimate
    # question starts as footers and the whole document gets zero questions.
    if f["has_question_marker"] or f["has_numbered_marker"]:
        confidence = 0.97 if f["has_question_marker"] else 0.82
        reasons.append("question_marker" if f["has_question_marker"] else "numbered_marker")
        return BlockClassification(block.block_id, "question_start", confidence, reasons)
    if f["is_footer"] or text in repeated_texts:
        return BlockClassification(block.block_id, "footer", 0.98, ["footer_or_repeated_short_text"])
    if f["has_subquestion_marker"]:
        return BlockClassification(block.block_id, "subquestion", 0.90, ["subquestion_marker"])
    if _RE_ANSWER_TABLE.search(text):
        return BlockClassification(block.block_id, "answer_table", 0.90, ["dense_answer_table"])
    if _RE_INLINE_SOLUTION.search(text):
        return BlockClassification(block.block_id, "solution_block", 0.84, ["solution_cue"])
    if f["looks_like_table"]:
        return BlockClassification(block.block_id, "table", 0.85, ["markdown_table"])
    if f["is_heading"] and _RE_TITLE.search(text):
        confidence = 0.92 if index < max(4, int(total * 0.15)) else 0.75
        return BlockClassification(block.block_id, "document_title", confidence, ["title_heading"])
    if f["is_heading"] and _RE_SECTION.search(text):
        return BlockClassification(block.block_id, "section_header", 0.88, ["section_heading"])
    if f["is_heading"]:
        return BlockClassification(block.block_id, "section_header", 0.70, ["generic_heading"])
    return BlockClassification(block.block_id, "unknown", 0.40, ["no_strong_signal"])


def _repeated_short_texts(blocks: list[DocumentBlock]) -> set[str]:
    counts: dict[str, set[int]] = {}
    for block in blocks:
        text = block.features["normalized_text"].strip()
        if 0 < len(text) <= 40:
            counts.setdefault(text, set()).add(block.page_num)
    return {text for text, pages in counts.items() if len(pages) >= 2}
