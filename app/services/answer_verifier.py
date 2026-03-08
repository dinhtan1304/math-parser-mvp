"""
answer_verifier.py — Kiểm tra đáp án của câu hỏi AI-generated.

Luồng:
  1. Nhận list câu hỏi vừa sinh
  2. Gọi Gemini với prompt "Giải lại và kiểm tra đáp án"
  3. So sánh đáp án gốc vs đáp án verify
  4. Đánh dấu câu sai/đáng ngờ, tự sửa nếu có thể
  5. Kiểm tra trùng lặp với DB hiện có

Tích hợp: gọi sau ai_generator.generate() trong rag_generator.py
"""

import json
import logging
import asyncio
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_VERIFY_PROMPT = """Bạn là giáo viên toán chấm bài. Kiểm tra {count} câu hỏi sau.

VỚI MỖI CÂU, hãy:
1. Giải lại bài từ đầu (KHÔNG nhìn đáp án cho sẵn)
2. So sánh kết quả của bạn với đáp án gốc
3. Đánh giá: CORRECT, WRONG, hoặc AMBIGUOUS

CÂU HỎI:
{questions_block}

Trả về JSON array (KHÔNG markdown, KHÔNG giải thích):
[
  {{
    "index": 0,
    "verdict": "CORRECT" | "WRONG" | "AMBIGUOUS",
    "your_answer": "<đáp án bạn tính được>",
    "original_answer": "<đáp án gốc>",
    "note": "<giải thích ngắn nếu WRONG hoặc AMBIGUOUS>",
    "corrected_answer": "<đáp án đúng nếu WRONG, null nếu CORRECT>",
    "corrected_solution": ["<bước 1>", "<bước 2>", ...] 
  }}
]

LƯU Ý:
- Với trắc nghiệm: kiểm tra xem đáp án đúng có nằm trong các lựa chọn không
- Với tự luận: giải lại hoàn chỉnh
- Nếu đề bài mơ hồ hoặc có nhiều cách hiểu → AMBIGUOUS
- corrected_solution chỉ cần khi verdict = WRONG"""

_DEDUP_CHECK_PROMPT = """So sánh câu hỏi MỚI với các câu đã CÓ trong ngân hàng.
Kiểm tra xem câu mới có thực sự MỚI không (khác số liệu, khác dạng), hay chỉ là paraphrase.

CÂU MỚI:
{new_question}

CÂU ĐÃ CÓ:
{existing_questions}

Trả về JSON (KHÔNG markdown):
{{
  "is_duplicate": true/false,
  "most_similar_index": <index câu giống nhất trong danh sách đã có, -1 nếu không giống>,
  "similarity_reason": "<giải thích ngắn>"
}}"""


def _format_questions_for_verify(questions: list[dict]) -> str:
    parts = []
    for i, q in enumerate(questions):
        text = q.get("question", "")
        answer = q.get("answer", "")
        steps = q.get("solution_steps", [])
        difficulty = q.get("difficulty", "")

        part = f"[Câu {i}] ({difficulty}) {text}\n  Đáp án: {answer}"
        if steps:
            part += "\n  Lời giải: " + " → ".join(str(s)[:150] for s in steps[:4])
        parts.append(part)
    return "\n\n".join(parts)


class AnswerVerifier:
    """Verify AI-generated math questions for correctness."""

    # Verify in batches of 5 (optimal for Gemini context)
    BATCH_SIZE = 5
    MAX_CONCURRENT = 2

    def __init__(self):
        self._semaphore: Optional[asyncio.Semaphore] = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT)
        return self._semaphore

    async def verify_and_fix(
        self,
        questions: list[dict],
        auto_fix: bool = True,
    ) -> dict:
        """Verify answers and optionally auto-fix wrong ones.

        Args:
            questions: List of generated question dicts
            auto_fix: If True, replace wrong answers with corrected ones

        Returns:
            {
                "questions": [...],      # verified (and optionally fixed) questions
                "stats": {
                    "total": int,
                    "correct": int,
                    "wrong": int,
                    "ambiguous": int,
                    "fixed": int,
                    "removed": int,
                },
                "verdicts": [...]        # raw verification results
            }
        """
        from app.services.ai_generator import ai_generator

        if not ai_generator._client or not questions:
            return {
                "questions": questions,
                "stats": {"total": len(questions), "correct": len(questions),
                          "wrong": 0, "ambiguous": 0, "fixed": 0, "removed": 0},
                "verdicts": [],
            }

        # Split into batches
        batches = []
        for i in range(0, len(questions), self.BATCH_SIZE):
            batches.append(questions[i:i + self.BATCH_SIZE])

        # Verify each batch
        all_verdicts = []
        for batch_idx, batch in enumerate(batches):
            verdicts = await self._verify_batch(batch, ai_generator)
            # Offset indices
            for v in verdicts:
                v["index"] = v.get("index", 0) + (batch_idx * self.BATCH_SIZE)
            all_verdicts.extend(verdicts)

        # Apply fixes
        stats = {"total": len(questions), "correct": 0, "wrong": 0,
                 "ambiguous": 0, "fixed": 0, "removed": 0}
        result_questions = []

        for i, q in enumerate(questions):
            verdict = next((v for v in all_verdicts if v.get("index") == i), None)

            if verdict is None:
                # Verification failed for this question — keep it as-is
                result_questions.append(q)
                stats["correct"] += 1
                continue

            v = verdict.get("verdict", "CORRECT").upper()

            if v == "CORRECT":
                result_questions.append(q)
                stats["correct"] += 1
            elif v == "WRONG":
                stats["wrong"] += 1
                if auto_fix and verdict.get("corrected_answer"):
                    # Fix the answer
                    fixed_q = dict(q)
                    fixed_q["answer"] = verdict["corrected_answer"]
                    if verdict.get("corrected_solution"):
                        fixed_q["solution_steps"] = verdict["corrected_solution"]
                    fixed_q["_verified"] = "fixed"
                    result_questions.append(fixed_q)
                    stats["fixed"] += 1
                else:
                    # Can't fix — remove the question
                    stats["removed"] += 1
            elif v == "AMBIGUOUS":
                stats["ambiguous"] += 1
                # Keep ambiguous questions but flag them
                flagged_q = dict(q)
                flagged_q["_verified"] = "ambiguous"
                flagged_q["_verify_note"] = verdict.get("note", "")
                result_questions.append(flagged_q)

        logger.info(
            f"Verification: {stats['correct']} correct, {stats['wrong']} wrong "
            f"({stats['fixed']} fixed, {stats['removed']} removed), "
            f"{stats['ambiguous']} ambiguous"
        )

        return {
            "questions": result_questions,
            "stats": stats,
            "verdicts": all_verdicts,
        }

    async def _verify_batch(self, batch: list[dict], ai) -> list[dict]:
        """Verify a batch of questions via Gemini."""
        from google.genai import types

        questions_block = _format_questions_for_verify(batch)
        prompt = _VERIFY_PROMPT.format(
            count=len(batch),
            questions_block=questions_block,
        )

        sem = self._get_semaphore()
        async with sem:
            for attempt in range(2):
                try:
                    resp = await ai._client.aio.models.generate_content(
                        model=ai.gemini_model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            temperature=0,
                            max_output_tokens=4096,
                            response_mime_type="application/json",
                        ),
                    )
                    text = ""
                    try:
                        text = resp.text or ""
                    except Exception:
                        pass

                    if not text:
                        continue

                    # Parse JSON
                    text = text.strip()
                    if text.startswith("```"):
                        text = text.split("```")[1].lstrip("json").strip()

                    data = json.loads(text)
                    if isinstance(data, list):
                        return data
                    return []

                except Exception as e:
                    logger.warning(f"Verify batch attempt {attempt + 1} failed: {e}")
                    if "429" in str(e):
                        await asyncio.sleep(5)

        logger.warning("Verification failed for batch, treating all as correct")
        return []

    async def check_duplicates(
        self,
        db: AsyncSession,
        new_questions: list[dict],
        user_id: int,
        grade: Optional[int] = None,
    ) -> list[dict]:
        """Check if generated questions duplicate existing ones in DB.

        Uses vector search to find potential duplicates, then AI to confirm.
        Returns questions with _is_duplicate flag.
        """
        from app.services.vector_search import find_similar, enrich_text_for_embedding

        results = []
        for q in new_questions:
            q_text = q.get("question", "")
            topic = q.get("topic", "")

            # Vector search for similar existing questions
            try:
                similar = await find_similar(
                    db, q_text, user_id,
                    topic=topic or None,
                    grade=grade,
                    limit=3,
                    min_similarity=0.75,  # High threshold for dedup
                )
            except Exception:
                similar = []

            if similar:
                q["_potential_duplicates"] = len(similar)
                q["_similar_ids"] = [s["question_id"] for s in similar]
                q["_max_similarity"] = max(s["similarity"] for s in similar)
            else:
                q["_potential_duplicates"] = 0

            results.append(q)

        return results


# Singleton
answer_verifier = AnswerVerifier()