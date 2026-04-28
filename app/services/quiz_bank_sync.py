"""
Quiz ↔ Bank sync: auto-save quiz questions to the question bank for reuse.

When a teacher creates quiz questions (manual or file import), a copy is
saved into the `question` table so they can be reused in future quizzes.
Deduplication is handled via content_hash (MD5 of normalized text).
"""

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.question import Question
from app.schemas.quiz import QuizQuestionCreate

logger = logging.getLogger(__name__)

# Quiz type → bank question_type
TYPE_MAP = {
    "multiple_choice": "TN",
    "checkbox": "TN",
    "true_false": "TN",
    "fill_blank": "TL",
    "reorder": "TL",
    "essay": "DS",
}

# Quiz difficulty → bank difficulty
DIFF_MAP = {
    "easy": "NB",
    "medium": "TH",
    "hard": "VD",
    "expert": "VDC",
}


def _content_hash(text: str) -> str:
    """MD5 hash of normalized question text for deduplication."""
    normalized = " ".join(text.strip().split()).lower()
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def _extract_answer_text(q_type: str, answer: Any, choices: Optional[list] = None) -> str:
    """Extract a plain-text answer string from the polymorphic quiz answer."""
    if answer is None:
        return ""

    if q_type == "multiple_choice":
        # answer is the correct choice key, e.g. "A"
        return str(answer)

    if q_type == "checkbox":
        # answer is a list of correct keys, e.g. ["A", "C"]
        if isinstance(answer, list):
            return ", ".join(str(k) for k in answer)
        return str(answer)

    if q_type == "true_false":
        # answer is True/False or "true"/"false"
        if isinstance(answer, bool):
            return "True" if answer else "False"
        return str(answer).capitalize()

    if q_type == "fill_blank":
        # answer can be a string or list of accepted values
        if isinstance(answer, list):
            return " | ".join(str(v) for v in answer)
        return str(answer)

    if q_type == "reorder":
        # answer is the correct order, e.g. [1, 3, 2, 4]
        if isinstance(answer, list):
            return ", ".join(str(v) for v in answer)
        return str(answer)

    if q_type == "essay":
        # essay may have a sample answer or be empty
        if isinstance(answer, str):
            return answer
        return str(answer) if answer else ""

    return str(answer) if answer else ""


async def save_to_bank(
    db: AsyncSession,
    user_id: int,
    q_data: QuizQuestionCreate,
    grade: Optional[int] = None,
) -> Question:
    """Create a bank Question from quiz question data. Returns existing if duplicate."""
    content_hash = _content_hash(q_data.question_text)

    # Check for existing duplicate by user + content_hash
    existing = await db.execute(
        select(Question).where(
            Question.user_id == user_id,
            Question.content_hash == content_hash,
        )
    )
    found = existing.scalars().first()
    if found:
        return found

    # Extract answer text
    choices_raw = [c.model_dump() for c in q_data.choices] if q_data.choices else None
    answer_text = _extract_answer_text(q_data.type, q_data.answer, choices_raw)

    # Solution steps → JSON string
    solution_str = json.dumps(q_data.solution.steps, ensure_ascii=False) if q_data.solution and q_data.solution.steps else "[]"

    bank_q = Question(
        user_id=user_id,
        question_text=q_data.question_text,
        question_type=TYPE_MAP.get(q_data.type, "TN"),
        difficulty=DIFF_MAP.get(q_data.difficulty or "medium", "TH"),
        subject_code=q_data.subject_code,
        answer=answer_text,
        solution_steps=solution_str,
        grade=grade,
        topic=q_data.tags[0] if q_data.tags else "",
        is_public=False,
        content_hash=content_hash,
    )
    db.add(bank_q)
    await db.flush()  # Get ID without committing
    return bank_q


async def save_to_bank_batch(
    db: AsyncSession,
    user_id: int,
    questions: List[QuizQuestionCreate],
    grade: Optional[int] = None,
) -> Dict[str, Question]:
    """Batch save questions to bank. Returns dict: content_hash → Question.

    Uses 1 SELECT for dedup check + 1 bulk flush for new questions.
    """
    # 1. Compute all hashes upfront
    hash_map: Dict[str, QuizQuestionCreate] = {}
    for q in questions:
        h = _content_hash(q.question_text)
        if h not in hash_map:
            hash_map[h] = q

    all_hashes = list(hash_map.keys())

    # 2. Single SELECT to find all existing
    result_map: Dict[str, Question] = {}
    if all_hashes:
        existing = await db.execute(
            select(Question).where(
                Question.user_id == user_id,
                Question.content_hash.in_(all_hashes),
            )
        )
        for q in existing.scalars().all():
            result_map[q.content_hash] = q

    # 3. Bulk insert only new questions
    new_questions: List[Question] = []
    for h, q_data in hash_map.items():
        if h in result_map:
            continue

        choices_raw = [c.model_dump() for c in q_data.choices] if q_data.choices else None
        answer_text = _extract_answer_text(q_data.type, q_data.answer, choices_raw)
        solution_str = json.dumps(q_data.solution.steps, ensure_ascii=False) if q_data.solution and q_data.solution.steps else "[]"

        bank_q = Question(
            user_id=user_id,
            question_text=q_data.question_text,
            question_type=TYPE_MAP.get(q_data.type, "TN"),
            difficulty=DIFF_MAP.get(q_data.difficulty or "medium", "TH"),
            subject_code=q_data.subject_code,
            answer=answer_text,
            solution_steps=solution_str,
            grade=grade,
            topic=q_data.tags[0] if q_data.tags else "",
            is_public=False,
            content_hash=h,
        )
        db.add(bank_q)
        new_questions.append(bank_q)
        result_map[h] = bank_q

    if new_questions:
        await db.flush()

    return result_map
