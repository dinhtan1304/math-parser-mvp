"""Regex pre-segmentation for Vietnamese K12 exam markdown.

Splits OCR markdown into ``QuestionBlock`` objects, one per `Câu N` /
`Bài N` / numbered item. The blocks are NOT a final parse — they are
input hints for the Gemini finalizer (see :mod:`gemini_finalizer`).

The segmenter is deterministic and offline. It deliberately avoids
classifying question type strictly: it emits a *hint* (e.g.
``true_false_candidate``) that Gemini can confirm or override.

Public API
----------
* :class:`QuestionBlock` — dataclass describing one segmented block.
* :func:`strip_answer_section` — peel off the trailing "ĐÁP ÁN" section
  so it can be handed to :mod:`answer_extractor` without polluting the
  body.
* :func:`split_into_blocks` — slice body markdown into ``QuestionBlock``
  candidates.
* :func:`annotate_hints` — fill ``hint_type``/``hint_options`` for a
  single block.
* :func:`segment` — main entry combining all three.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger(__name__)


# ── Marker patterns ───────────────────────────────────────────────
# Question opener. Tolerant to whitespace + leading zero + variant
# punctuation after the number.  Examples accepted:
#   "Câu 1.", "Câu 1:", "Câu  01 .", "Bài 2)", "Câu 3 :"
_RE_MARKER_PRIMARY = re.compile(
    r"(?m)^\s*(?P<marker>Câu|Bài)\s*0*(?P<num>\d{1,3})\s*[.:)\]]?\s*",
)

# Numeric-only opener, used only when no Câu/Bài found in the body.
#   "1." "1)" "12)" — but NOT "1.5" (would match section numbers like
#   "1.2 Định nghĩa").  We accept exactly one digit-run + closer.
_RE_MARKER_NUMERIC = re.compile(
    r"(?m)^\s*(?P<num>\d{1,3})\s*[.)]\s+(?=\S)",
)

# Trailing answer section: "ĐÁP ÁN", "Đáp án", "ANSWER KEY".
_RE_ANSWER_SECTION = re.compile(
    r"(?im)^\s*(?:ĐÁP\s*ÁN|Đáp\s*án|ANSWER\s*KEY|BẢNG\s*ĐÁP\s*ÁN)\s*[:.]?\s*$",
)

# Option markers inside a block. Two flavors:
#   - "A." / "A)" / "A:" (multiple-choice)
#   - "a)" / "b)" / "c)" / "d)" (Vietnamese true/false multi-part)
_RE_OPTION_UPPER = re.compile(r"(?m)^\s*([ABCD])\s*[.):]\s*(.+?)(?=\n\s*[ABCD]\s*[.):]|\Z)", re.DOTALL)
_RE_OPTION_LOWER = re.compile(r"(?m)^\s*([abcd])\s*\)\s*(.+?)(?=\n\s*[abcd]\s*\)|\Z)", re.DOTALL)

# Image embed reference in markdown.
_RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]+\)")

# True/false signals — Vietnamese exam style.
_RE_TRUE_FALSE_SIGNAL = re.compile(r"\b(?:Đúng|Sai|đúng|sai|TRUE|FALSE)\b")


@dataclass
class QuestionBlock:
    """One segmented question candidate.

    Attributes:
        number: Question number extracted from the marker.
        raw_block: Full text from this marker up to the next marker
            (or end-of-body). Includes the original marker line.
        hint_type: Coarse hint for Gemini — one of
            ``'tn_4choice'``, ``'true_false_candidate'``,
            ``'no_options'``, ``'essay_long'``, ``'unknown'``.
        hint_options: Best-effort A/B/C/D extraction. May be wrong;
            Gemini is expected to fix it.
        image_refs: Raw `![alt](path)` strings found in the block.
        source_offset: Byte offset where the block starts in the
            original markdown (handy for debugging).
        marker_kind: ``'cau'``, ``'bai'`` or ``'numeric'``.
    """

    number: int
    raw_block: str
    hint_type: str = "unknown"
    hint_options: dict[str, str] = field(default_factory=dict)
    image_refs: list[str] = field(default_factory=list)
    source_offset: int = 0
    marker_kind: str = "cau"


def strip_answer_section(markdown: str) -> tuple[str, str]:
    """Split markdown at the first "ĐÁP ÁN" header it finds.

    Returns ``(body, answer_section)``. If no header is present, the
    whole markdown is returned as the body and ``answer_section`` is
    empty. The answer section is preserved verbatim so the existing
    :class:`AnswerExtractor` can do its job.
    """
    if not markdown:
        return markdown, ""
    match = _RE_ANSWER_SECTION.search(markdown)
    if not match:
        return markdown, ""
    body = markdown[: match.start()].rstrip()
    answer = markdown[match.start():].strip()
    return body, answer


def _iter_primary_markers(markdown: str) -> list[tuple[int, int, str, str]]:
    """Return ``(start, end_of_match, num_str, marker_kind)`` tuples."""
    hits: list[tuple[int, int, str, str]] = []
    for m in _RE_MARKER_PRIMARY.finditer(markdown):
        kind = "cau" if m.group("marker").lower() == "câu" else "bai"
        hits.append((m.start(), m.end(), m.group("num"), kind))
    return hits


def _iter_numeric_markers(markdown: str) -> list[tuple[int, int, str, str]]:
    hits: list[tuple[int, int, str, str]] = []
    for m in _RE_MARKER_NUMERIC.finditer(markdown):
        hits.append((m.start(), m.end(), m.group("num"), "numeric"))
    return hits


def split_into_blocks(markdown: str, fallback_numeric: bool = True) -> list[QuestionBlock]:
    """Split markdown into question blocks.

    Prefers ``Câu``/``Bài`` markers. If none are found and
    ``fallback_numeric`` is true, falls back to plain ``1.``/``1)``
    numeric openers — but only when at least three of them appear, to
    avoid false splits on enumerated bullets like ``1) ...`` inside a
    single question.
    """
    if not markdown:
        return []
    hits = _iter_primary_markers(markdown)
    if not hits and fallback_numeric:
        candidate = _iter_numeric_markers(markdown)
        if len(candidate) >= 3:
            hits = candidate
            logger.info("regex_segmenter: using numeric fallback (%d markers)", len(candidate))
    if not hits:
        return []
    blocks: list[QuestionBlock] = []
    for i, (start, marker_end, num_str, kind) in enumerate(hits):
        block_end = hits[i + 1][0] if i + 1 < len(hits) else len(markdown)
        try:
            number = int(num_str)
        except ValueError:
            continue
        raw = markdown[start:block_end].strip()
        blocks.append(
            QuestionBlock(
                number=number,
                raw_block=raw,
                source_offset=start,
                marker_kind=kind,
            )
        )
    return blocks


def _extract_options(block_text: str) -> tuple[dict[str, str], str]:
    """Try to extract A/B/C/D options.

    Returns ``(options, flavor)`` where ``flavor`` is ``'upper'``,
    ``'lower'`` or ``''``. Lowercase a/b/c/d is the Vietnamese
    true-false-multi-part convention.
    """
    upper: dict[str, str] = {}
    for m in _RE_OPTION_UPPER.finditer(block_text):
        upper[m.group(1)] = re.sub(r"\s+", " ", m.group(2)).strip()[:600]
    if len(upper) >= 2:
        return upper, "upper"
    lower: dict[str, str] = {}
    for m in _RE_OPTION_LOWER.finditer(block_text):
        lower[m.group(1).upper()] = re.sub(r"\s+", " ", m.group(2)).strip()[:600]
    if len(lower) >= 2:
        return lower, "lower"
    return {}, ""


def annotate_hints(
    block: QuestionBlock,
    min_options_for_tn: int = 3,
    essay_long_min_chars: int = 300,
) -> QuestionBlock:
    """Populate ``hint_type``, ``hint_options`` and ``image_refs``.

    Hints are intentionally coarse — Gemini still makes the final call
    on ``type``. We only flag the obvious cases:

      * three or more upper-case options → ``tn_4choice``;
      * lower-case options + Đúng/Sai signal → ``true_false_candidate``;
      * no options + long body → ``essay_long``;
      * no options + short body → ``no_options`` (likely short_answer);
      * everything else → ``unknown``.
    """
    text = block.raw_block
    block.image_refs = _RE_IMAGE.findall(text)
    options, flavor = _extract_options(text)
    block.hint_options = options
    body_no_images = _RE_IMAGE.sub("", text)
    body_chars = len(body_no_images.strip())

    if flavor == "upper" and len(options) >= min_options_for_tn:
        block.hint_type = "tn_4choice"
    elif flavor == "lower" and len(options) >= 2 and _RE_TRUE_FALSE_SIGNAL.search(text):
        block.hint_type = "true_false_candidate"
    elif not options:
        if body_chars >= essay_long_min_chars:
            block.hint_type = "essay_long"
        else:
            block.hint_type = "no_options"
    else:
        block.hint_type = "unknown"
    return block


def annotate_all(
    blocks: Iterable[QuestionBlock],
    min_options_for_tn: int = 3,
    essay_long_min_chars: int = 300,
) -> list[QuestionBlock]:
    return [annotate_hints(b, min_options_for_tn, essay_long_min_chars) for b in blocks]


def segment(
    markdown: str,
    fallback_numeric: bool = True,
    min_options_for_tn: int = 3,
    essay_long_min_chars: int = 300,
) -> tuple[list[QuestionBlock], str]:
    """Main entry — strip answer section, split, annotate.

    Returns ``(blocks, answer_section_text)``.
    """
    body, answer_section = strip_answer_section(markdown)
    blocks = split_into_blocks(body, fallback_numeric=fallback_numeric)
    blocks = annotate_all(blocks, min_options_for_tn=min_options_for_tn, essay_long_min_chars=essay_long_min_chars)
    logger.info(
        "regex_segmenter: %d blocks (hints: %s); answer_section=%d chars",
        len(blocks),
        {h: sum(1 for b in blocks if b.hint_type == h) for h in {b.hint_type for b in blocks}},
        len(answer_section),
    )
    return blocks, answer_section


def detect_numbering_gaps(blocks: list[QuestionBlock]) -> list[int]:
    """Return missing question numbers between ``min`` and ``max``."""
    if not blocks:
        return []
    nums = sorted({b.number for b in blocks})
    lo, hi = nums[0], nums[-1]
    present = set(nums)
    return [n for n in range(lo, hi + 1) if n not in present]
