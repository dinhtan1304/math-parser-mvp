from __future__ import annotations

from dataclasses import dataclass, field

from app.services.block_classifier import BlockClassification
from app.services.document_blocks import DocumentBlock, DocumentPage


@dataclass
class QuestionNode:
    question_id: str
    cau_num: int | None
    blocks: list[DocumentBlock] = field(default_factory=list)
    inline_solution_blocks: list[DocumentBlock] = field(default_factory=list)


@dataclass
class ParsedDocument:
    pages: list[DocumentPage]
    questions: list[QuestionNode]
    answer_blocks_by_num: dict[int, list[DocumentBlock]]
    answer_tables: list[DocumentBlock]
    boilerplate: list[DocumentBlock]
    unmatched_blocks: list[DocumentBlock]
    unmatched_solution_blocks: list[DocumentBlock]
    document_type: str
    warnings: list[str]
    confidence: float
    section_count: int


def build_document_structure(
    pages: list[DocumentPage],
    classifications: dict[str, BlockClassification],
) -> ParsedDocument:
    blocks = [block for page in pages for block in page.blocks]
    questions: list[QuestionNode] = []
    answer_blocks_by_num: dict[int, list[DocumentBlock]] = {}
    answer_tables: list[DocumentBlock] = []
    boilerplate: list[DocumentBlock] = []
    unmatched_blocks: list[DocumentBlock] = []
    unmatched_solution_blocks: list[DocumentBlock] = []
    warnings: list[str] = []
    current_question: QuestionNode | None = None
    current_answer_num: int | None = None
    in_answer_section = False
    section_count = 0

    for block in blocks:
        role = classifications[block.block_id].role
        if role in {"boilerplate", "footer", "metadata"}:
            boilerplate.append(block)
            continue
        if role == "answer_key_header":
            in_answer_section = True
            current_answer_num = None
            section_count += 1
            continue
        if role == "answer_table":
            answer_tables.append(block)
            continue
        if role == "question_start":
            qnum = block.features.get("question_num")
            if in_answer_section:
                current_answer_num = qnum
                if qnum is not None:
                    answer_blocks_by_num.setdefault(qnum, []).append(block)
                else:
                    unmatched_solution_blocks.append(block)
                continue
            current_question = QuestionNode(
                question_id=f"q{len(questions) + 1}",
                cau_num=qnum,
                blocks=[block],
            )
            questions.append(current_question)
            continue
        if in_answer_section:
            if current_answer_num is not None:
                answer_blocks_by_num.setdefault(current_answer_num, []).append(block)
            elif role == "solution_block":
                unmatched_solution_blocks.append(block)
            else:
                unmatched_blocks.append(block)
            continue
        if current_question is not None:
            if role == "solution_block":
                current_question.inline_solution_blocks.append(block)
            else:
                current_question.blocks.append(block)
        else:
            unmatched_blocks.append(block)

    if not questions:
        warnings.append("no_questions_detected")
    duplicate_nums = _duplicate_numbers(questions)
    if duplicate_nums:
        warnings.append(f"duplicate_question_numbers={sorted(duplicate_nums)}")
    if in_answer_section and not answer_blocks_by_num and not answer_tables:
        warnings.append("answer_section_without_mappable_content")
    if unmatched_solution_blocks:
        warnings.append(f"unmatched_solution_blocks={len(unmatched_solution_blocks)}")

    document_type = _infer_document_type(
        questions=questions,
        answer_blocks_by_num=answer_blocks_by_num,
        answer_tables=answer_tables,
    )
    confidence = _estimate_confidence(
        questions=questions,
        warnings=warnings,
        classifications=classifications,
    )
    return ParsedDocument(
        pages=pages,
        questions=questions,
        answer_blocks_by_num=answer_blocks_by_num,
        answer_tables=answer_tables,
        boilerplate=boilerplate,
        unmatched_blocks=unmatched_blocks,
        unmatched_solution_blocks=unmatched_solution_blocks,
        document_type=document_type,
        warnings=warnings,
        confidence=confidence,
        section_count=section_count,
    )


def _duplicate_numbers(questions: list[QuestionNode]) -> set[int]:
    seen: set[int] = set()
    dupes: set[int] = set()
    for q in questions:
        if q.cau_num is None:
            continue
        if q.cau_num in seen:
            dupes.add(q.cau_num)
        seen.add(q.cau_num)
    return dupes


def _infer_document_type(
    *,
    questions: list[QuestionNode],
    answer_blocks_by_num: dict[int, list[DocumentBlock]],
    answer_tables: list[DocumentBlock],
) -> str:
    if not questions:
        return "no_questions"
    if answer_blocks_by_num:
        return "exam_with_full_solutions"
    if any(q.inline_solution_blocks for q in questions):
        return "questions_with_inline_solutions"
    if answer_tables:
        return "exam_with_answer_key"
    return "questions_only"


def _estimate_confidence(
    *,
    questions: list[QuestionNode],
    warnings: list[str],
    classifications: dict[str, BlockClassification],
) -> float:
    if not questions:
        return 0.0
    q_scores = [
        classifications[q.blocks[0].block_id].confidence
        for q in questions
        if q.blocks
    ]
    base = sum(q_scores) / max(len(q_scores), 1)
    penalty = min(0.45, len(warnings) * 0.08)
    return round(max(0.0, min(1.0, base - penalty)), 3)
