"""Thin wrapper around :class:`AIQuestionParser` for K12 batch use.

The CLI feeds *pre-segmented* markdown to Gemini: ``regex_segmenter``
has already split the document into well-formed ``Câu N`` blocks, so
Gemini's job is reduced to (a) emitting JSON matching
:class:`GeneratedQuestion` and (b) cleaning up any OCR residue
(typos, fragmented options, slightly broken LaTeX).

This module does two extra things on top of ``AIQuestionParser.parse``:

* prepends a short subject + grade hint so Gemini biases toward the
  correct ``subject_code`` / ``grade`` fields;
* re-injects ``![](path)`` image references that the LLM sometimes drops
  when it normalizes question text.
"""

from __future__ import annotations

import logging
import re
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.ai_parser import AIQuestionParser
    from app.services.k12_batch.regex_segmenter import QuestionBlock

logger = logging.getLogger(__name__)


_SUBJECT_LABELS = {
    "toan": "Toán",
    "vat-li": "Vật lý",
    "ly": "Vật lý",
    "hoa-hoc": "Hóa học",
    "hoa": "Hóa học",
    "sinh-hoc": "Sinh học",
    "sinh": "Sinh học",
    "ngu-van": "Ngữ văn",
    "van": "Ngữ văn",
    "lich-su": "Lịch sử",
    "su": "Lịch sử",
    "dia-li": "Địa lý",
    "dia": "Địa lý",
    "tieng-anh": "Tiếng Anh",
    "anh": "Tiếng Anh",
}


_RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]+\)")


def subject_label(subject_code: str) -> str:
    """Map slug → Vietnamese display name (with sensible fallback)."""
    return _SUBJECT_LABELS.get(subject_code.lower(), subject_code)


def _normalize_image_key(image_md: str) -> str:
    """Take just the path inside ``![alt](path)`` for matching."""
    m = re.match(r"!\[[^\]]*\]\(([^)]+)\)", image_md)
    return (m.group(1).strip() if m else image_md).rsplit("/", 1)[-1]


def assemble_input_text(
    blocks: list["QuestionBlock"],
    subject_code: str,
    grade: int,
    inject_hint: bool = True,
) -> str:
    """Build the markdown handed to ``AIQuestionParser.parse``.

    Blocks are joined back together with their ``Câu N.`` markers
    intact; the parser's own chunker (``_parse_chunked_parallel``) will
    handle further splitting if needed. A one-line subject/grade hint
    is prepended so Gemini knows the context.
    """
    parts: list[str] = []
    if inject_hint:
        parts.append(f"[K12 hint] Đây là đề thi môn {subject_label(subject_code)} lớp {grade}. Giữ nguyên LaTeX và link ảnh trong các câu hỏi.")
        parts.append("")
    for block in blocks:
        parts.append(block.raw_block.strip())
        parts.append("")  # blank line separator
    return "\n".join(parts).strip()


def merge_image_refs(parsed: list[dict[str, Any]], blocks: list["QuestionBlock"]) -> list[dict[str, Any]]:
    """Re-insert any image markdown that Gemini may have dropped.

    Matches parsed items to source blocks by question number, then
    appends missing ``![](path)`` strings to the ``question`` field.
    Idempotent: if Gemini already kept the reference, this is a no-op.
    """
    if not parsed or not blocks:
        return parsed
    block_by_num: dict[int, "QuestionBlock"] = {b.number: b for b in blocks}
    # Also try matching by source order as a fallback for items whose
    # numbers Gemini renumbered.
    fallback_iter = iter(blocks)
    for item in parsed:
        q_text = str(item.get("question") or "")
        if not q_text:
            continue
        number = _extract_question_number(q_text)
        block = block_by_num.get(number) if number is not None else None
        if block is None:
            block = next(fallback_iter, None)
        if block is None or not block.image_refs:
            continue
        existing_keys = {_normalize_image_key(m) for m in _RE_IMAGE.findall(q_text)}
        missing = [img for img in block.image_refs if _normalize_image_key(img) not in existing_keys]
        if missing:
            item["question"] = q_text.rstrip() + "\n" + "\n".join(missing)
    return parsed


_RE_LEADING_NUM = re.compile(r"^\s*(?:Câu|Bài|Question)?\s*(\d{1,3})\b")


def _extract_question_number(text: str) -> int | None:
    m = _RE_LEADING_NUM.match(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _apply_defaults(item: dict[str, Any], subject_code: str, grade: int) -> dict[str, Any]:
    """Fill missing fields with caller-provided defaults.

    Gemini occasionally returns ``subject_code = "toan"`` regardless of
    the hint, especially for short questions. Override when blank or
    inconsistent — caller is authoritative.
    """
    item.setdefault("question", "")
    item.setdefault("type", "TN")
    item.setdefault("topic", "")
    item.setdefault("difficulty", "TH")
    item.setdefault("chapter", "")
    item.setdefault("lesson_title", "")
    item.setdefault("answer", "")
    item.setdefault("solution_steps", [])
    # `subject` in the Gemini schema → map to `subject_code` for
    # `GeneratedQuestion`.
    if "subject" in item and "subject_code" not in item:
        item["subject_code"] = item.pop("subject")
    if not item.get("subject_code"):
        item["subject_code"] = subject_code
    if not item.get("grade"):
        item["grade"] = grade
    # Normalize solution_steps to list[str] — Gemini sometimes returns a
    # single string.
    steps = item.get("solution_steps")
    if isinstance(steps, str):
        item["solution_steps"] = [steps] if steps.strip() else []
    elif not isinstance(steps, list):
        item["solution_steps"] = []
    return item


async def finalize(
    blocks: list["QuestionBlock"],
    subject_code: str,
    grade: int,
    ai_parser: "AIQuestionParser",
    inject_hint: bool = True,
) -> list[dict[str, Any]]:
    """Run pre-segmented blocks through ``AIQuestionParser`` → JSON list.

    Returned dicts are shaped for :class:`GeneratedQuestion`
    construction (``question``, ``type``, ``subject_code``, ``grade``,
    ``topic``, ``difficulty``, ``chapter``, ``lesson_title``,
    ``answer``, ``solution_steps``).
    """
    if not blocks:
        return []
    text = assemble_input_text(blocks, subject_code, grade, inject_hint=inject_hint)
    parsed = await ai_parser.parse(text=text, subject_hint=subject_code)
    if not parsed:
        logger.warning("gemini_finalizer: ai_parser returned 0 questions for %d blocks", len(blocks))
        return []
    parsed = [_apply_defaults(item, subject_code, grade) for item in parsed]
    parsed = merge_image_refs(parsed, blocks)
    logger.info("gemini_finalizer: %d blocks → %d questions", len(blocks), len(parsed))
    return parsed
