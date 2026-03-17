"""
question_generator.py — Sinh câu hỏi tương tự từ danh sách câu hỏi mẫu.

Delegates to AIQuestionGenerator (ai_generator.py) which has battle-tested
3-tier Gemini fallback with robust JSON repair.
"""

from typing import List, Dict
from collections import Counter

from app.services.ai_generator import ai_generator


def _infer_params(questions: List[Dict]) -> tuple[str, str, str]:
    """Infer q_type, topic, difficulty from the majority of source questions."""
    types       = Counter(q.get("question_type") or q.get("type", "TN") for q in questions)
    diffs       = Counter(q.get("difficulty", "TH") for q in questions)
    chapters    = Counter(q.get("chapter", "") for q in questions)

    q_type     = types.most_common(1)[0][0] if types else "TN"
    difficulty = diffs.most_common(1)[0][0] if diffs else "TH"
    # Use most common chapter as topic hint (ai_generator uses it as "Chủ đề" in prompt)
    topic      = chapters.most_common(1)[0][0] if chapters else "Toán học"

    return q_type, topic, difficulty


async def generate_similar_questions(
    source_questions: List[Dict],
    count: int,
    gemini_api_key: str,       # kept for API compat; ai_generator reads key from env
    gemini_model: str = "gemini-2.5-flash",  # kept for API compat; ignored
) -> List[Dict]:
    """
    Sinh `count` câu hỏi tương tự từ `source_questions`.

    source_questions: list dict với keys question_text, question_type,
                      difficulty, grade, chapter, lesson_title, answer
    Trả về list dict với keys question, type, difficulty, grade,
                chapter, lesson_title, answer, solution_steps, topic
    """
    q_type, topic, difficulty = _infer_params(source_questions)

    results = await ai_generator.generate(
        samples    = source_questions,
        count      = count,
        q_type     = q_type,
        topic      = topic,
        difficulty = difficulty,
    )

    return results
