from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.services.answer_extractor import AnswerExtractor
from app.services.document_structure import ParsedDocument, QuestionNode

logger = logging.getLogger(__name__)


_RE_FINAL_ANSWER = re.compile(
    r"(?i)(?:đáp\s*số|kết\s*quả)\s*[:.]?\s*([^\n]{1,120})"
)


@dataclass
class AssemblyResult:
    questions: list[dict]
    unmatched_answer_blocks: list[str]
    unmatched_solution_blocks: list[str]
    # Phase 3.11: SỐ câu có đáp án (trong answer key) nhưng KHÔNG khớp câu hỏi
    # nào được trích → đáp án bị bỏ thầm lặng. Surface để caller cảnh báo.
    unmatched_answer_nums: list[int] = field(default_factory=list)
    skipped_empty: int = 0


def assemble_questions(
    parsed: ParsedDocument,
    *,
    full_text: str,
    image_map: dict[str, str] | None = None,
) -> AssemblyResult:
    image_map = image_map or {}
    rows: list[dict] = []
    question_nums = {
        q.cau_num
        for q in parsed.questions
        if q.cau_num is not None
    }
    provisional = [
        {"cau_num": q.cau_num or idx + 1, "text": _join_question_text(q)}
        for idx, q in enumerate(parsed.questions)
    ]
    answer_map = AnswerExtractor().extract(full_text, provisional)

    skipped_empty = 0
    for idx, q in enumerate(parsed.questions):
        cau_num = q.cau_num or idx + 1
        question_text = _join_question_text(q)
        # Phase 3.11: bỏ câu rỗng (mọi block trắng) — trước đây lọt vào output với
        # text="" làm hỏng downstream (Gemini classify cần text non-empty).
        if not question_text.strip():
            skipped_empty += 1
            logger.warning(
                "assemble_questions: skip empty question cau_num=%s block_ids=%s",
                cau_num, [block.block_id for block in q.blocks],
            )
            continue
        answer = None
        answer_source = None
        if answer_map.confidence >= 0.8 and cau_num in answer_map.answers:
            answer = answer_map.answers[cau_num]
            answer_source = answer_map.source

        solution_blocks = parsed.answer_blocks_by_num.get(cau_num, [])
        solution_text = _join_blocks(solution_blocks[1:] if solution_blocks else [])
        if not solution_text and q.inline_solution_blocks:
            solution_text = _join_blocks(q.inline_solution_blocks)
        solution_steps = _solution_to_steps(solution_text)

        if not answer and solution_text:
            final_answer = _extract_final_answer(solution_text)
            if final_answer:
                answer = final_answer
                answer_source = "answer_key"

        q_images = {
            placeholder: path
            for placeholder, path in image_map.items()
            if placeholder in question_text
        }
        bbox = _bbox_union([block.bbox for block in q.blocks if block.bbox])
        rows.append(
            {
                "cau_num": cau_num,
                "text": question_text,
                "answer": answer,
                "answer_source": answer_source,
                "images": q_images,
                "solution_steps": solution_steps,
                "source_block_ids": [block.block_id for block in q.blocks],
                "bbox": list(bbox) if bbox else None,
                "page_num": q.blocks[0].page_num if q.blocks else None,
                "segmentation_confidence": parsed.confidence,
            }
        )

    unmatched_answer_nums = sorted(
        num for num in parsed.answer_blocks_by_num if num not in question_nums
    )
    if unmatched_answer_nums:
        logger.warning(
            "assemble_questions: %d answer-key entries không khớp câu hỏi (cau_num=%s) "
            "→ đáp án có thể bị mất",
            len(unmatched_answer_nums), unmatched_answer_nums[:20],
        )

    return AssemblyResult(
        questions=rows,
        unmatched_answer_blocks=[
            block.block_id
            for num, blocks in parsed.answer_blocks_by_num.items()
            if num not in question_nums
            for block in blocks
        ],
        unmatched_solution_blocks=[b.block_id for b in parsed.unmatched_solution_blocks],
        unmatched_answer_nums=unmatched_answer_nums,
        skipped_empty=skipped_empty,
    )


def _join_question_text(q: QuestionNode) -> str:
    return _join_blocks(q.blocks)


def _join_blocks(blocks) -> str:
    return "\n\n".join(block.text.strip() for block in blocks if block.text.strip()).strip()


def _solution_to_steps(solution_text: str, max_steps: int = 30) -> list[str]:
    if not solution_text.strip():
        return []
    paragraphs = re.split(r"\n\s*\n", solution_text.strip())
    steps: list[str] = []
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > 500:
            steps.extend(line.strip() for line in paragraph.splitlines() if line.strip())
        else:
            steps.append(paragraph)
        if len(steps) >= max_steps:
            break
    return steps[:max_steps]


def _extract_final_answer(solution_text: str) -> str | None:
    match = _RE_FINAL_ANSWER.search(solution_text)
    if not match:
        return None
    return match.group(1).strip().rstrip(".,;:")


def _bbox_union(boxes) -> tuple[float, float, float, float] | None:
    normalized = [tuple(box) for box in boxes if box and len(box) == 4]
    if not normalized:
        return None
    return (
        min(box[0] for box in normalized),
        min(box[1] for box in normalized),
        max(box[2] for box in normalized),
        max(box[3] for box in normalized),
    )
