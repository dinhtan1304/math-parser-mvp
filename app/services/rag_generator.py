"""
rag_generator.py — RAG-powered question generation.

Luồng:
  1. Parse prompt tự do → structured criteria (grade, chapters, difficulty mix)
  2. Vector search PER CHAPTER → sample đa dạng theo đúng chủ đề
  3. Gọi ai_generator với sample đã retrieve

Ví dụ:
  prompt = "Tạo 10 câu TN lớp 8 ôn hằng đẳng thức và phân thức, mix NB/TH/VD"
  →  grade=8, chapters=["C2.Hằng đẳng thức","C6.Phân thức"]
     difficulty_mix={"NB":3,"TH":4,"VD":3}, question_type="TN"
  →  vector search lớp 8 C2 → 3 sample
     vector search lớp 8 C6 → 3 sample
  →  generate 10 câu dùng 6 sample trên làm context
"""

import json
import logging
import asyncio
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

logger = logging.getLogger(__name__)

# ── Prompt để LLM parse yêu cầu tự do ──────────────────────────────────────
_PARSE_PROMPT = """Phân tích yêu cầu sinh đề toán sau và trả về JSON.

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
TOÁN 6: C1.Số tự nhiên|C2.Tính chia hết|C3.Số nguyên|C4.Hình phẳng|C5.Phân số|C6.Số thập phân
TOÁN 7: C1.Số hữu tỉ|C2.Số thực|C3.Góc/đường thẳng|C4.Tam giác bằng nhau|C5.Thống kê|C6.Tỉ lệ thức|C7.Đại số
TOÁN 8: C1.Đa thức|C2.Hằng đẳng thức|C3.Tứ giác|C4.Định lí Thales|C5.Dữ liệu|C6.Phân thức|C7.PT bậc nhất|C8.Xác suất
TOÁN 9: C1.Hệ PT|C2.Bất PT|C3.Căn thức|C4.Hệ thức lượng|C5.Đường tròn|C6.Hàm y=ax²|C7.Tần số|C8.Xác suất
TOÁN 10: C1.Mệnh đề/tập hợp|C2.BPT bậc nhất 2 ẩn|C3.Hệ thức lượng tam giác|C4.Vectơ|C5.Thống kê|C6.Hàm bậc hai|C7.Tọa độ phẳng
TOÁN 11: C1.Lượng giác|C2.Dãy số/cấp số|C3.Thống kê ghép|C4.Song song KG|C5.Giới hạn/liên tục|C6.Hàm mũ/logarit|C7.Vuông góc KG|C9.Đạo hàm
TOÁN 12: C1.Ứng dụng đạo hàm/đồ thị|C2.Vectơ KG|C3.Phân tán|C4.Nguyên hàm/tích phân|C5.Tọa độ KG|C6.Xác suất có điều kiện

JSON:"""


async def _parse_prompt_to_criteria(prompt: str, client, model: str) -> dict:
    """Gọi Gemini để parse free-text prompt → structured criteria dict."""
    from google.genai import types

    full_prompt = _PARSE_PROMPT.format(prompt=prompt)

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

            # Strip markdown fences if any
            text = text.strip()
            if text.startswith("```"):
                text = text.split("```")[1].lstrip("json").strip()

            data = json.loads(text)
            logger.info(f"Parsed criteria ({label}): {data}")
            return data
        except Exception as e:
            logger.warning(f"Criteria parse {label} failed: {e}")

    # Fallback: return minimal criteria
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
    """Vector search cho một chapter cụ thể, fallback sang SQL nếu cần."""
    from app.db.models.question import Question

    samples = []

    # ── Try vector search first ──
    try:
        from app.services.vector_search import find_similar
        query = f"Toán {grade or ''} {chapter_hint}"
        similar = await find_similar(
            db, query, user_id,
            difficulty=difficulty or None,
            limit=limit,
            min_similarity=0.25,   # lower threshold for chapter-level search
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

    # ── Fallback: SQL ILIKE match on chapter/topic ──
    try:
        conditions = [Question.user_id == user_id]
        if grade:
            conditions.append(Question.grade == grade)

        # Extract keyword from chapter hint (e.g. "C2.Hằng đẳng thức" → "Hằng đẳng thức")
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

    # Fill missing levels
    for lvl in levels:
        mix.setdefault(lvl, 0)

    current_total = sum(mix.values())
    if current_total == 0:
        # Default 30/30/30/10
        nb = max(1, round(total * 0.3))
        th = max(1, round(total * 0.3))
        vd = max(1, round(total * 0.3))
        vdc = max(0, total - nb - th - vd)
        mix = {"NB": nb, "TH": th, "VD": vd, "VDC": vdc}
        current_total = sum(mix.values())

    if current_total != total:
        # Scale proportionally
        factor = total / current_total
        adjusted = {k: round(v * factor) for k, v in mix.items()}
        # Fix rounding error on TH (most common)
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
) -> dict:
    """
    Main entry point cho RAG generation.

    Returns:
        {
          "questions": [...],
          "criteria": {...},   # parsed criteria cho FE hiển thị
          "sample_count": int,
          "message": str,
        }
    """
    from app.services.ai_generator import ai_generator

    if not ai_generator._client:
        raise RuntimeError("GOOGLE_API_KEY chưa được cấu hình.")

    # ── Step 1: Parse prompt → criteria ──
    raw_criteria = await _parse_prompt_to_criteria(
        prompt, ai_generator._client, ai_generator.gemini_model
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

    # ── Step 2: Retrieve samples per chapter + per difficulty ──
    sample_tasks = []

    if chapters:
        # Lấy sample cho từng chapter (tối đa 3 sample/chapter)
        samples_per_chapter = max(2, 8 // len(chapters))
        for ch in chapters:
            # Lấy sample không lọc theo difficulty để đa dạng
            sample_tasks.append(
                _retrieve_samples_for_chapter(db, ch, grade, None, user_id, samples_per_chapter)
            )
    else:
        # Không biết chapter → search theo topic_hint chung
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
        # Prefer samples matching this difficulty, fallback to all samples
        diff_samples = [s for s in all_samples if s.get("difficulty") == difficulty]
        if not diff_samples:
            diff_samples = all_samples

        # Build a topic string for this section
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

    # Build human-readable message
    chapters_str = ", ".join(c.split(".", 1)[-1] for c in chapters) if chapters else topic_hint
    mix_str = " + ".join(f"{v} {k}" for k, v in diff_mix.items())
    message = (
        f"Sinh {len(all_questions)}/{total} câu {q_type}"
        + (f" lớp {grade}" if grade else "")
        + (f" — {chapters_str}" if chapters_str else "")
        + f" ({mix_str})"
        + (f" · {len(all_samples)} câu mẫu từ ngân hàng" if all_samples else " · không có câu mẫu")
    )

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
    }