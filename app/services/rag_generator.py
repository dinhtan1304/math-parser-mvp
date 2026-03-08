"""
rag_generator.py — RAG-powered question generation.

v3 changes:
  1. Curriculum pulled from DB (not hardcoded) via _build_curriculum_summary()
  2. Answer verification after generation via answer_verifier
  3. Duplicate check against existing question bank
  4. Grade parameter passed to vector search for better filtering

Luồng:
  1. Parse prompt tự do → structured criteria (grade, chapters, difficulty mix)
  2. Vector search PER CHAPTER → sample đa dạng theo đúng chủ đề
  3. Gọi ai_generator với sample đã retrieve
  4. Verify answers (optional, enabled by default)
  5. Check duplicates against existing bank
"""

import json
import logging
import asyncio
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

logger = logging.getLogger(__name__)

# ── Cache curriculum summary string (rebuilt on first call per process) ──
_curriculum_summary_cache: Optional[str] = None


async def _build_curriculum_summary(db: AsyncSession) -> str:
    """Build curriculum summary from DB instead of hardcoding.

    Generates a compact string like:
      TOÁN 6: C1.Tập hợp các số tự nhiên|C2.Tính chia hết|C3.Số nguyên|...
      TOÁN 7: C1.Số hữu tỉ|C2.Số thực|...

    Cached in memory after first call.
    """
    global _curriculum_summary_cache
    if _curriculum_summary_cache:
        return _curriculum_summary_cache

    try:
        from app.db.models.curriculum import Curriculum
        rows = (await db.execute(
            select(Curriculum.grade, Curriculum.chapter_no, Curriculum.chapter)
            .where(Curriculum.is_active == True)
            .distinct()
            .order_by(Curriculum.grade, Curriculum.chapter_no)
        )).fetchall()

        if not rows:
            logger.warning("No curriculum data in DB, using empty summary")
            _curriculum_summary_cache = "(Chưa có dữ liệu chương trình)"
            return _curriculum_summary_cache

        # Group by grade
        by_grade: dict[int, list[str]] = {}
        seen: set[tuple[int, int]] = set()
        for grade, chapter_no, chapter_name in rows:
            key = (grade, chapter_no)
            if key in seen:
                continue
            seen.add(key)
            # Extract short name: "Chương I. Tập hợp các số tự nhiên" → "Tập hợp các số tự nhiên"
            short = chapter_name
            if "." in short:
                short = short.split(".", 1)[1].strip()
            by_grade.setdefault(grade, []).append(f"C{chapter_no}.{short}")

        lines = []
        for grade in sorted(by_grade.keys()):
            chapters = "|".join(by_grade[grade])
            lines.append(f"TOÁN {grade}: {chapters}")

        _curriculum_summary_cache = "\n".join(lines)
        logger.info(f"Curriculum summary built: {len(seen)} chapters across {len(by_grade)} grades")
        return _curriculum_summary_cache

    except Exception as e:
        logger.warning(f"Failed to build curriculum summary from DB: {e}")
        _curriculum_summary_cache = "(Lỗi tải chương trình)"
        return _curriculum_summary_cache


def invalidate_curriculum_cache():
    """Call when curriculum data changes."""
    global _curriculum_summary_cache
    _curriculum_summary_cache = None


# ── Prompt template (curriculum placeholder filled dynamically) ──
_PARSE_PROMPT_TEMPLATE = """Phân tích yêu cầu sinh đề toán sau và trả về JSON.

YÊU CẦU: "{prompt}"

Trả về JSON object (KHÔNG có markdown, KHÔNG giải thích):
{{
  "grade": <số nguyên 6-12 hoặc null nếu không rõ>,
  "chapters": ["C1.Tên", "C2.Tên", ...],
  "difficulty_mix": {{"NB": <n>, "TH": <n>, "VD": <n>, "VDC": <n>}},
  "question_type": "<TN hoặc TL hoặc mixed>",
  "total_count": <tổng số câu, mặc định 10>,
  "topic_hint": "<từ khoá chủ đề chính cho tìm kiếm>"
}}

QUY TẮC:
- difficulty_mix: tổng = total_count. Nếu không nói rõ thì dùng tỉ lệ 30/30/30/10
- chapters: dùng format "C<số>.<tên ngắn>", ví dụ "C2.Hằng đẳng thức"
- question_type: "TN" nếu nói trắc nghiệm, "TL" nếu tự luận, "TN" nếu không rõ
- Nếu người dùng chỉ nói chủ đề (không nói chương), hãy suy luận chapters từ chương trình GDPT

Chương trình GDPT 2018:
{curriculum}

JSON:"""


async def _parse_prompt_to_criteria(prompt: str, client, model: str, db: AsyncSession) -> dict:
    """Gọi Gemini để parse free-text prompt → structured criteria dict.

    v3: curriculum pulled from DB dynamically.
    """
    from google.genai import types

    curriculum_summary = await _build_curriculum_summary(db)
    full_prompt = _PARSE_PROMPT_TEMPLATE.format(
        prompt=prompt,
        curriculum=curriculum_summary,
    )

    for mime, label in [("application/json", "JSON"), (None, "plain")]:
        try:
            cfg = types.GenerateContentConfig(
                temperature=0,
                max_output_tokens=512,
                **({"response_mime_type": mime} if mime else {}),
            )
            resp = await client.aio.models.generate_content(
                model=model, contents=full_prompt, config=cfg,
            )
            text = ""
            try:
                text = resp.text
            except Exception:
                pass
            if not text:
                continue

            text = text.strip()
            if text.startswith("```"):
                text = text.split("```")[1].lstrip("json").strip()

            data = json.loads(text)
            logger.info(f"Parsed criteria ({label}): {data}")
            return data
        except Exception as e:
            logger.warning(f"Criteria parse {label} failed: {e}")

    logger.warning("Criteria parse failed, using defaults")
    return {
        "grade": None, "chapters": [], "difficulty_mix": {"NB": 3, "TH": 4, "VD": 2, "VDC": 1},
        "question_type": "TN", "total_count": 10, "topic_hint": prompt[:100],
    }


async def _retrieve_samples_for_chapter(
    db: AsyncSession,
    chapter_hint: str,
    grade: Optional[int],
    difficulty: Optional[str],
    user_id: int,
    limit: int = 3,
) -> list[dict]:
    """Vector search cho một chapter cụ thể, fallback sang SQL nếu cần.

    v3: passes grade to find_similar for better filtering.
    """
    from app.db.models.question import Question

    samples = []

    # ── Try vector search first ──
    try:
        from app.services.vector_search import find_similar
        query = f"Toán {grade or ''} {chapter_hint}"
        similar = await find_similar(
            db, query, user_id,
            difficulty=difficulty or None,
            grade=grade,  # v3: filter by grade at DB level
            limit=limit,
            min_similarity=0.25,
        )
        if similar:
            ids = [s["question_id"] for s in similar]
            rows = (await db.execute(
                select(Question).where(Question.id.in_(ids))
            )).scalars().all()
            samples = [_q_to_dict(q) for q in rows]
            logger.debug(f"Vector: {len(samples)} samples for '{chapter_hint}'")
    except Exception as e:
        logger.debug(f"Vector search skipped for '{chapter_hint}': {e}")

    if samples:
        return samples

    # ── Fallback: SQL ILIKE match ──
    try:
        conditions = [Question.user_id == user_id]
        if grade:
            conditions.append(Question.grade == grade)

        keyword = chapter_hint.split(".", 1)[-1].strip() if "." in chapter_hint else chapter_hint
        if keyword:
            from sqlalchemy import or_
            conditions.append(or_(
                Question.chapter.ilike(f"%{keyword}%"),
                Question.topic.ilike(f"%{keyword}%"),
                Question.lesson_title.ilike(f"%{keyword}%"),
            ))
        if difficulty:
            conditions.append(Question.difficulty == difficulty)

        rows = (await db.execute(
            select(Question)
            .where(and_(*conditions))
            .order_by(Question.created_at.desc())
            .limit(limit)
        )).scalars().all()
        samples = [_q_to_dict(q) for q in rows]
        logger.debug(f"SQL fallback: {len(samples)} samples for '{chapter_hint}'")
    except Exception as e:
        logger.warning(f"SQL fallback failed for '{chapter_hint}': {e}")

    return samples


def _q_to_dict(q) -> dict:
    return {
        "question_text": q.question_text,
        "type": q.question_type or "TN",
        "topic": q.topic or "",
        "difficulty": q.difficulty or "TH",
        "grade": q.grade,
        "chapter": q.chapter or "",
        "answer": q.answer or "",
        "solution_steps": q.solution_steps or "[]",
    }


def _normalize_difficulty_mix(mix: dict, total: int) -> dict:
    """Đảm bảo tổng difficulty_mix == total, không có giá trị âm."""
    levels = ["NB", "TH", "VD", "VDC"]
    mix = {k: max(0, int(v)) for k, v in mix.items() if k in levels}

    for lvl in levels:
        mix.setdefault(lvl, 0)

    current_total = sum(mix.values())
    if current_total == 0:
        nb = max(1, round(total * 0.3))
        th = max(1, round(total * 0.3))
        vd = max(1, round(total * 0.3))
        vdc = max(0, total - nb - th - vd)
        mix = {"NB": nb, "TH": th, "VD": vd, "VDC": vdc}
        current_total = sum(mix.values())

    if current_total != total:
        factor = total / current_total
        adjusted = {k: round(v * factor) for k, v in mix.items()}
        diff = total - sum(adjusted.values())
        adjusted["TH"] = max(0, adjusted["TH"] + diff)
        mix = adjusted

    return {k: v for k, v in mix.items() if v > 0}


async def generate_from_prompt(
    db: AsyncSession,
    prompt: str,
    user_id: int,
    grade_override: Optional[int] = None,
    count_override: Optional[int] = None,
    verify_answers: bool = True,
) -> dict:
    """
    Main entry point cho RAG generation.

    v3 changes:
      - verify_answers: enable answer verification (default True)
      - Curriculum from DB
      - Grade-aware vector search
      - Duplicate detection

    Returns:
        {
          "questions": [...],
          "criteria": {...},
          "sample_count": int,
          "message": str,
          "verification": {...} | None,   # v3: verification stats
        }
    """
    from app.services.ai_generator import ai_generator

    if not ai_generator._client:
        raise RuntimeError("GOOGLE_API_KEY chưa được cấu hình.")

    # ── Step 1: Parse prompt → criteria (with curriculum from DB) ──
    raw_criteria = await _parse_prompt_to_criteria(
        prompt, ai_generator._client, ai_generator.gemini_model, db
    )

    grade = grade_override or raw_criteria.get("grade")
    total = count_override or int(raw_criteria.get("total_count") or 10)
    total = max(1, min(50, total))
    chapters = raw_criteria.get("chapters") or []
    q_type = raw_criteria.get("question_type") or "TN"
    topic_hint = raw_criteria.get("topic_hint") or prompt[:100]
    diff_mix = _normalize_difficulty_mix(
        raw_criteria.get("difficulty_mix") or {}, total
    )

    logger.info(
        f"RAG criteria: grade={grade}, chapters={chapters}, "
        f"type={q_type}, mix={diff_mix}, total={total}"
    )

    # ── Step 2: Retrieve samples per chapter ──
    sample_tasks = []

    if chapters:
        samples_per_chapter = max(2, 8 // len(chapters))
        for ch in chapters:
            sample_tasks.append(
                _retrieve_samples_for_chapter(db, ch, grade, None, user_id, samples_per_chapter)
            )
    else:
        sample_tasks.append(
            _retrieve_samples_for_chapter(db, topic_hint, grade, None, user_id, 6)
        )

    results = await asyncio.gather(*sample_tasks, return_exceptions=True)

    all_samples: list[dict] = []
    seen_texts: set[str] = set()
    for res in results:
        if isinstance(res, Exception):
            continue
        for s in res:
            key = (s.get("question_text") or "")[:80]
            if key not in seen_texts:
                seen_texts.add(key)
                all_samples.append(s)

    logger.info(f"RAG retrieved {len(all_samples)} unique samples")

    # ── Step 3: Generate per difficulty section ──
    gen_tasks = []
    task_labels = []

    for difficulty, count in diff_mix.items():
        if count <= 0:
            continue
        diff_samples = [s for s in all_samples if s.get("difficulty") == difficulty]
        if not diff_samples:
            diff_samples = all_samples

        chapter_names = [c.split(".", 1)[-1] for c in chapters] if chapters else [topic_hint]
        section_topic = f"Toán {grade or ''} - {', '.join(chapter_names)}"

        gen_tasks.append(ai_generator.generate(
            samples=diff_samples[:5],
            count=count,
            q_type=q_type,
            topic=section_topic,
            difficulty=difficulty,
        ))
        task_labels.append(f"{count}×{difficulty}")

    if not gen_tasks:
        raise RuntimeError("Không thể xác định cấu trúc đề từ yêu cầu.")

    logger.info(f"Generating sections: {', '.join(task_labels)}")
    gen_results = await asyncio.gather(*gen_tasks, return_exceptions=True)

    all_questions = []
    for i, res in enumerate(gen_results):
        if isinstance(res, Exception):
            logger.error(f"Section {task_labels[i]} failed: {res}")
        else:
            all_questions.extend(res)

    # ── Step 4: Answer verification (v3) ──
    verification = None
    if verify_answers and all_questions:
        try:
            from app.services.answer_verifier import answer_verifier
            verify_result = await answer_verifier.verify_and_fix(
                all_questions, auto_fix=True
            )
            all_questions = verify_result["questions"]
            verification = verify_result["stats"]
            logger.info(f"Verification: {verification}")
        except Exception as e:
            logger.warning(f"Answer verification skipped: {e}")

    # ── Step 5: Duplicate check (v3) ──
    try:
        from app.services.answer_verifier import answer_verifier
        all_questions = await answer_verifier.check_duplicates(
            db, all_questions, user_id, grade=grade
        )
    except Exception as e:
        logger.debug(f"Duplicate check skipped: {e}")

    # Build message
    chapters_str = ", ".join(c.split(".", 1)[-1] for c in chapters) if chapters else topic_hint
    mix_str = " + ".join(f"{v} {k}" for k, v in diff_mix.items())
    message = (
        f"Sinh {len(all_questions)}/{total} câu {q_type}"
        + (f" lớp {grade}" if grade else "")
        + (f" — {chapters_str}" if chapters_str else "")
        + f" ({mix_str})"
        + (f" · {len(all_samples)} câu mẫu từ ngân hàng" if all_samples else " · không có câu mẫu")
    )
    if verification:
        v = verification
        if v.get("fixed", 0) > 0 or v.get("removed", 0) > 0:
            message += f" · Kiểm tra: {v.get('fixed',0)} sửa, {v.get('removed',0)} loại"

    return {
        "questions": all_questions,
        "criteria": {
            "grade": grade,
            "chapters": chapters,
            "difficulty_mix": diff_mix,
            "question_type": q_type,
            "total_count": total,
            "topic_hint": topic_hint,
        },
        "sample_count": len(all_samples),
        "message": message,
        "verification": verification,
    }