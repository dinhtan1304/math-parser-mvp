"""
chat_rag.py — RAG-powered chatbot hỗ trợ học sinh giải toán.

v3 changes:
  - Grade-aware: session stores grade context, vector search filters by grade
  - Grade auto-detection from first user message
  - Enriched vector search query
  - Better context formatting with solution steps

Luồng mỗi tin nhắn:
  1. Detect/use grade context from session
  2. Embed câu hỏi + vector search (filtered by grade)
  3. Đưa context vào prompt → Gemini giải thích
  4. Trả về answer + danh sách câu tham khảo
"""

import json
import re
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── Grade detection pattern ──
_RE_GRADE = re.compile(r'lớp\s*(\d{1,2})', re.IGNORECASE)

# ── System prompt ──
_CHAT_SYSTEM = """Bạn là gia sư toán thông minh, hỗ trợ học sinh THCS/THPT Việt Nam.

NGUYÊN TẮC:
- Giải thích TỪNG BƯỚC, rõ ràng, dễ hiểu
- Dùng LaTeX cho tất cả biểu thức toán ($...$ inline, $$...$$ display)
- Khuyến khích học sinh tự suy nghĩ — đặt câu hỏi gợi mở nếu phù hợp
- Trả lời bằng tiếng Việt
- Nếu có câu hỏi tương tự trong ngân hàng, đề cập đến nhưng không copy nguyên xi
- Ngắn gọn — tối đa 5 bước, mỗi bước 1-2 câu

KHÔNG làm:
- Không cho đáp án ngay khi học sinh chưa thử
- Không giải những bài ngoài phạm vi toán học"""

_CHAT_PROMPT = """Học sinh hỏi: {question}

{context_block}

Hãy giải thích rõ ràng từng bước."""

_CONTEXT_BLOCK_TEMPLATE = """CÂU HỎI TƯƠNG TỰ TRONG NGÂN HÀNG (dùng làm tham khảo, không copy):
{similar_questions}"""


def _detect_grade(text_content: str) -> Optional[int]:
    """Detect grade from user message. E.g. 'em đang học lớp 8' → 8"""
    m = _RE_GRADE.search(text_content)
    if m:
        g = int(m.group(1))
        if 6 <= g <= 12:
            return g
    return None


def _format_similar(questions: list[dict]) -> str:
    parts = []
    for i, q in enumerate(questions, 1):
        q_text = q.get("question_text", "")[:300]
        answer = q.get("answer", "")
        steps = q.get("solution_steps_parsed") or []
        grade = q.get("grade")
        topic = q.get("topic", "")

        header = f"[{i}]"
        if grade:
            header += f" (Lớp {grade}"
            if topic:
                header += f" - {topic}"
            header += ")"

        part = f"{header} {q_text}"
        if steps:
            part += f"\n    Lời giải mẫu: " + " → ".join(str(s)[:100] for s in steps[:3])
        elif answer:
            part += f"\n    Đáp án: {answer[:100]}"
        parts.append(part)
    return "\n\n".join(parts)


async def _get_similar_questions(
    db: AsyncSession,
    query: str,
    user_id: int,
    grade: Optional[int] = None,
    limit: int = 3,
) -> list[dict]:
    """Vector search câu hỏi tương tự, with grade filtering, fallback sang FTS."""
    from app.db.models.question import Question
    from sqlalchemy import select

    results = []

    # ── Try vector search (with grade filter) ──
    try:
        from app.services.vector_search import find_similar
        similar = await find_similar(
            db, query, user_id,
            grade=grade,  # v3: filter by grade
            limit=limit,
            min_similarity=0.3,
        )
        if similar:
            ids = [s["question_id"] for s in similar]
            rows = (await db.execute(
                select(Question).where(Question.id.in_(ids))
            )).scalars().all()
            results = [_row_to_dict(q) for q in rows if q.solution_steps]
            logger.debug(f"Chat vector: {len(results)} similar (grade={grade})")
    except Exception as e:
        logger.debug(f"Chat vector search skipped: {e}")

    # If grade filter returned nothing, try without grade
    if not results and grade:
        try:
            from app.services.vector_search import find_similar
            similar = await find_similar(
                db, query, user_id,
                grade=None,  # broaden search
                limit=limit,
                min_similarity=0.35,
            )
            if similar:
                ids = [s["question_id"] for s in similar]
                rows = (await db.execute(
                    select(Question).where(Question.id.in_(ids))
                )).scalars().all()
                results = [_row_to_dict(q) for q in rows if q.solution_steps]
                logger.debug(f"Chat vector (no grade filter): {len(results)} similar")
        except Exception:
            pass

    if results:
        return results

    # ── Fallback: keyword search ──
    try:
        keywords = " ".join(query.split()[:6])
        conditions = [
            "user_id = :uid",
            "question_text ILIKE :kw",
            "solution_steps IS NOT NULL",
        ]
        params = {"uid": user_id, "kw": f"%{keywords[:30]}%"}

        if grade:
            conditions.append("grade = :grade")
            params["grade"] = grade

        where = " AND ".join(conditions)
        rows = (await db.execute(
            text(f"SELECT id, question_text, topic, difficulty, grade, answer, solution_steps FROM question WHERE {where} LIMIT :lim"),
            {**params, "lim": limit}
        )).fetchall()
        results = [_row_to_dict_from_tuple(r) for r in rows]
        logger.debug(f"Chat keyword fallback: {len(results)} questions")
    except Exception as e:
        logger.debug(f"Chat keyword fallback skipped: {e}")

    return results


def _row_to_dict(q) -> dict:
    steps = []
    if q.solution_steps:
        try:
            steps = json.loads(q.solution_steps)
        except Exception:
            steps = [q.solution_steps]
    return {
        "id": q.id,
        "question_text": q.question_text,
        "topic": q.topic or "",
        "difficulty": q.difficulty or "",
        "grade": q.grade,
        "answer": q.answer or "",
        "solution_steps_parsed": steps,
    }


def _row_to_dict_from_tuple(r) -> dict:
    steps = []
    if r[6]:
        try:
            steps = json.loads(r[6])
        except Exception:
            steps = [r[6]]
    return {
        "id": r[0],
        "question_text": r[1],
        "topic": r[2] or "",
        "difficulty": r[3] or "",
        "grade": r[4],
        "answer": r[5] or "",
        "solution_steps_parsed": steps,
    }


# ── DB helpers for session/message tables ───────────────────────────────────

async def ensure_chat_tables(engine) -> None:
    """Tạo bảng chat nếu chưa có — gọi lúc startup.

    v3: Added grade column to chat_session for grade-aware context.
    """
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_session (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                title TEXT DEFAULT 'Cuộc trò chuyện mới',
                grade INTEGER,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_message (
                id SERIAL PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user','assistant')),
                content TEXT NOT NULL,
                context_question_ids TEXT DEFAULT '[]',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_chat_session_user
            ON chat_session(user_id)
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_chat_message_session
            ON chat_message(session_id)
        """))

        # v3: Add grade column if upgrading
        try:
            await conn.execute(text(
                "ALTER TABLE chat_session ADD COLUMN grade INTEGER"
            ))
        except Exception:
            pass  # Already exists

    logger.info("Chat tables ready")


async def get_or_create_session(
    db: AsyncSession, user_id: int, session_id: Optional[int] = None
) -> int:
    if session_id:
        row = (await db.execute(
            text("SELECT id FROM chat_session WHERE id=:sid AND user_id=:uid"),
            {"sid": session_id, "uid": user_id}
        )).fetchone()
        if row:
            return row[0]

    row = (await db.execute(
        text("""
            INSERT INTO chat_session (user_id, title)
            VALUES (:uid, 'Cuộc trò chuyện mới')
            RETURNING id
        """),
        {"uid": user_id}
    )).fetchone()
    await db.commit()
    return row[0]


async def _get_session_grade(db: AsyncSession, session_id: int) -> Optional[int]:
    """Get grade context from session."""
    row = (await db.execute(
        text("SELECT grade FROM chat_session WHERE id = :sid"),
        {"sid": session_id}
    )).fetchone()
    return row[0] if row else None


async def _set_session_grade(db: AsyncSession, session_id: int, grade: int) -> None:
    """Store detected grade in session for future messages."""
    await db.execute(
        text("UPDATE chat_session SET grade = :grade WHERE id = :sid"),
        {"grade": grade, "sid": session_id}
    )
    # Don't commit here — will be committed with messages


async def load_history(db: AsyncSession, session_id: int, limit: int = 10) -> list[dict]:
    rows = (await db.execute(
        text("""
            SELECT role, content FROM chat_message
            WHERE session_id = :sid
            ORDER BY created_at DESC
            LIMIT :lim
        """),
        {"sid": session_id, "lim": limit}
    )).fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


async def save_messages(
    db: AsyncSession,
    session_id: int,
    user_content: str,
    assistant_content: str,
    context_ids: list[int],
) -> None:
    ids_json = json.dumps(context_ids)
    await db.execute(
        text("""
            INSERT INTO chat_message (session_id, role, content, context_question_ids)
            VALUES (:sid, 'user', :content, '[]')
        """),
        {"sid": session_id, "content": user_content}
    )
    await db.execute(
        text("""
            INSERT INTO chat_message (session_id, role, content, context_question_ids)
            VALUES (:sid, 'assistant', :content, :ids)
        """),
        {"sid": session_id, "content": assistant_content, "ids": ids_json}
    )
    await db.execute(
        text("""
            UPDATE chat_session
            SET updated_at = NOW(),
                title = CASE
                    WHEN title = 'Cuộc trò chuyện mới'
                    THEN LEFT(:title, 60)
                    ELSE title
                END
            WHERE id = :sid
        """),
        {"sid": session_id, "title": user_content}
    )
    await db.commit()


# ── Main chat function ───────────────────────────────────────────────────────

async def chat(
    db: AsyncSession,
    user_message: str,
    user_id: int,
    session_id: Optional[int] = None,
    grade: Optional[int] = None,
) -> dict:
    """
    Xử lý một tin nhắn chat.

    v3: grade-aware context.
      - grade param: explicit grade from FE (e.g. student profile)
      - Auto-detects grade from message text ("em học lớp 8")
      - Stores grade in session for subsequent messages

    Returns:
        {
          "answer": str,
          "session_id": int,
          "context_questions": [...],
          "detected_grade": int | None,   # v3
        }
    """
    from app.services.ai_generator import ai_generator

    if not ai_generator._client:
        raise RuntimeError("GOOGLE_API_KEY chưa được cấu hình.")

    # ── Step 1: Session ──
    session_id = await get_or_create_session(db, user_id, session_id)

    # ── Step 2: Resolve grade context ──
    effective_grade = grade

    # Try to get from session (previous messages)
    if not effective_grade:
        effective_grade = await _get_session_grade(db, session_id)

    # Try to detect from current message
    detected = _detect_grade(user_message)
    if detected:
        effective_grade = detected

    # Store grade in session if newly detected
    if effective_grade:
        await _set_session_grade(db, session_id, effective_grade)

    # ── Step 3: Load history ──
    history = await load_history(db, session_id, limit=12)

    # ── Step 4: RAG — tìm câu hỏi tương tự (grade-filtered) ──
    similar = await _get_similar_questions(
        db, user_message, user_id,
        grade=effective_grade,
        limit=3,
    )

    # ── Step 5: Build prompt ──
    if similar:
        context_block = _CONTEXT_BLOCK_TEMPLATE.format(
            similar_questions=_format_similar(similar)
        )
    else:
        context_block = ""

    user_prompt = _CHAT_PROMPT.format(
        question=user_message,
        context_block=context_block,
    ).strip()

    # ── Step 6: Build messages with history ──
    messages = []
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_prompt})

    # ── Step 7: Call Gemini ──
    from google.genai import types

    gemini_contents = []
    for msg in messages:
        gemini_contents.append(
            types.Content(
                role=msg["role"],
                parts=[types.Part(text=msg["content"])]
            )
        )

    # Add grade context to system prompt if available
    system = _CHAT_SYSTEM
    if effective_grade:
        system += f"\n\nHọc sinh đang học LỚP {effective_grade}. Điều chỉnh độ khó và cách giải thích phù hợp."

    answer = ""
    for attempt in range(2):
        try:
            resp = await ai_generator._client.aio.models.generate_content(
                model=ai_generator.gemini_model,
                contents=gemini_contents,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.4,
                    max_output_tokens=1024,
                ),
            )
            try:
                answer = resp.text or ""
            except Exception:
                answer = ""
            if answer:
                break
        except Exception as e:
            logger.warning(f"Chat Gemini attempt {attempt+1} failed: {e}")

    if not answer:
        answer = "Xin lỗi, mình gặp lỗi khi xử lý. Bạn thử hỏi lại nhé!"

    # ── Step 8: Save ──
    context_ids = [q["id"] for q in similar]
    await save_messages(db, session_id, user_message, answer, context_ids)

    return {
        "answer": answer,
        "session_id": session_id,
        "detected_grade": effective_grade,
        "context_questions": [
            {
                "id": q["id"],
                "question_text": q["question_text"][:200],
                "topic": q["topic"],
                "difficulty": q["difficulty"],
                "grade": q["grade"],
            }
            for q in similar
        ],
    }