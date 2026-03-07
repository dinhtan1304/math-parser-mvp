"""
chat_rag.py — RAG-powered chatbot hỗ trợ học sinh giải toán.

Luồng mỗi tin nhắn:
  1. Embed câu hỏi của học sinh
  2. Vector search → tìm câu hỏi tương tự trong ngân hàng (có solution_steps)
  3. Đưa context đó vào prompt → Gemini giải thích
  4. Trả về answer + danh sách câu tham khảo

Lưu lịch sử chat trong bảng chat_session + chat_message (tạo tự động).
"""

import json
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── System prompt cho chatbot ───────────────────────────────────────────────
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


def _format_similar(questions: list[dict]) -> str:
    parts = []
    for i, q in enumerate(questions, 1):
        text = q.get("question_text", "")[:300]
        answer = q.get("answer", "")
        steps = q.get("solution_steps_parsed") or []

        part = f"[{i}] {text}"
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
    limit: int = 3,
) -> list[dict]:
    """Vector search câu hỏi tương tự, fallback sang FTS."""
    from app.db.models.question import Question
    from sqlalchemy import select

    results = []

    # ── Try vector search ──
    try:
        from app.services.vector_search import find_similar
        similar = await find_similar(
            db, query, user_id,
            limit=limit,
            min_similarity=0.3,
        )
        if similar:
            ids = [s["question_id"] for s in similar]
            rows = (await db.execute(
                select(Question).where(Question.id.in_(ids))
            )).scalars().all()
            results = [_row_to_dict(q) for q in rows if q.solution_steps]
            logger.debug(f"Chat vector: {len(results)} similar questions")
    except Exception as e:
        logger.debug(f"Chat vector search skipped: {e}")

    if results:
        return results

    # ── Fallback: keyword search ──
    try:
        keywords = " ".join(query.split()[:6])
        rows = (await db.execute(
            select(Question)
            .where(
                Question.user_id == user_id,
                Question.question_text.ilike(f"%{keywords[:30]}%"),
                Question.solution_steps.isnot(None),
            )
            .limit(limit)
        )).scalars().all()
        results = [_row_to_dict(q) for q in rows]
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


# ── DB helpers for session/message tables ───────────────────────────────────

async def ensure_chat_tables(engine) -> None:
    """Tạo bảng chat nếu chưa có — gọi lúc startup."""
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS chat_session (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                title TEXT DEFAULT 'Cuộc trò chuyện mới',
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
    logger.info("Chat tables ready")


async def get_or_create_session(
    db: AsyncSession, user_id: int, session_id: Optional[int] = None
) -> int:
    """Lấy session hiện có hoặc tạo mới."""
    if session_id:
        row = (await db.execute(
            text("SELECT id FROM chat_session WHERE id=:sid AND user_id=:uid"),
            {"sid": session_id, "uid": user_id}
        )).fetchone()
        if row:
            return row[0]

    # Tạo session mới
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


async def load_history(db: AsyncSession, session_id: int, limit: int = 10) -> list[dict]:
    """Lấy N tin nhắn gần nhất của session."""
    rows = (await db.execute(
        text("""
            SELECT role, content FROM chat_message
            WHERE session_id = :sid
            ORDER BY created_at DESC
            LIMIT :lim
        """),
        {"sid": session_id, "lim": limit}
    )).fetchall()
    # Reverse để đúng thứ tự
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


async def save_messages(
    db: AsyncSession,
    session_id: int,
    user_content: str,
    assistant_content: str,
    context_ids: list[int],
) -> None:
    """Lưu cặp user/assistant message."""
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
    # Update session timestamp + auto-title từ tin đầu tiên
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
) -> dict:
    """
    Xử lý một tin nhắn chat.

    Returns:
        {
          "answer": str,
          "session_id": int,
          "context_questions": [...],   # câu hỏi tương tự dùng làm context
        }
    """
    from app.services.ai_generator import ai_generator

    if not ai_generator._client:
        raise RuntimeError("GOOGLE_API_KEY chưa được cấu hình.")

    # ── Step 1: Đảm bảo có session ──
    session_id = await get_or_create_session(db, user_id, session_id)

    # ── Step 2: Load lịch sử (tối đa 6 cặp = 12 tin) ──
    history = await load_history(db, session_id, limit=12)

    # ── Step 3: RAG — tìm câu hỏi tương tự ──
    similar = await _get_similar_questions(db, user_message, user_id, limit=3)

    # ── Step 4: Build prompt ──
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

    # ── Step 5: Build messages với history ──
    messages = []
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_prompt})

    # ── Step 6: Call Gemini ──
    from google.genai import types

    # Convert history format cho Gemini
    gemini_contents = []
    for msg in messages:
        gemini_contents.append(
            types.Content(
                role=msg["role"],
                parts=[types.Part(text=msg["content"])]
            )
        )

    answer = ""
    for attempt in range(2):
        try:
            resp = await ai_generator._client.aio.models.generate_content(
                model=ai_generator.gemini_model,
                contents=gemini_contents,
                config=types.GenerateContentConfig(
                    system_instruction=_CHAT_SYSTEM,
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

    # ── Step 7: Lưu tin nhắn ──
    context_ids = [q["id"] for q in similar]
    await save_messages(db, session_id, user_message, answer, context_ids)

    return {
        "answer": answer,
        "session_id": session_id,
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