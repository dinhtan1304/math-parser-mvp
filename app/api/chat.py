"""
chat.py — Chatbot hỗ trợ học sinh giải toán (RAG-powered).

Endpoints:
    POST /chat/message          — Gửi tin nhắn, nhận trả lời
    GET  /chat/sessions         — Danh sách session của user
    GET  /chat/sessions/{id}    — Lịch sử một session
    DELETE /chat/sessions/{id}  — Xoá session
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.db.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────

class ChatMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: Optional[int] = Field(default=None, description="None = tạo session mới")


class ContextQuestion(BaseModel):
    id: int
    question_text: str
    topic: str = ""
    difficulty: str = ""
    grade: Optional[int] = None


class ChatMessageResponse(BaseModel):
    answer: str
    session_id: int
    context_questions: list[ContextQuestion] = []


class SessionSummary(BaseModel):
    id: int
    title: str
    updated_at: str
    message_count: int = 0


class SessionHistory(BaseModel):
    session_id: int
    title: str
    messages: list[dict]


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/message", response_model=ChatMessageResponse)
async def send_message(
    req: ChatMessageRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Gửi tin nhắn đến chatbot toán.

    - Tự động tạo session mới nếu `session_id` là null
    - RAG tìm câu hỏi tương tự trong ngân hàng làm context
    - Trả về lời giải từng bước + danh sách câu tham khảo
    """
    from app.services.chat_rag import chat as _chat

    try:
        result = await _chat(
            db=db,
            user_message=req.message,
            user_id=current_user.id,
            session_id=req.session_id,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Chat failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Chat xử lý thất bại, thử lại sau.")

    return ChatMessageResponse(
        answer=result["answer"],
        session_id=result["session_id"],
        context_questions=[ContextQuestion(**q) for q in result["context_questions"]],
    )


@router.get("/sessions", response_model=list[SessionSummary])
async def list_sessions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Danh sách session chat của user, mới nhất trước."""
    rows = (await db.execute(
        text("""
            SELECT s.id, s.title, s.updated_at,
                   COUNT(m.id) as message_count
            FROM chat_session s
            LEFT JOIN chat_message m ON m.session_id = s.id
            WHERE s.user_id = :uid
            GROUP BY s.id, s.title, s.updated_at
            ORDER BY s.updated_at DESC
            LIMIT 50
        """),
        {"uid": current_user.id}
    )).fetchall()

    return [
        SessionSummary(
            id=r[0],
            title=r[1] or "Cuộc trò chuyện mới",
            updated_at=r[2].isoformat() if r[2] else "",
            message_count=r[3] or 0,
        )
        for r in rows
    ]


@router.get("/sessions/{session_id}", response_model=SessionHistory)
async def get_session(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lấy toàn bộ lịch sử của một session."""
    # Verify ownership
    session = (await db.execute(
        text("SELECT id, title FROM chat_session WHERE id=:sid AND user_id=:uid"),
        {"sid": session_id, "uid": current_user.id}
    )).fetchone()
    if not session:
        raise HTTPException(status_code=404, detail="Session không tồn tại")

    messages = (await db.execute(
        text("""
            SELECT role, content, created_at
            FROM chat_message
            WHERE session_id = :sid
            ORDER BY created_at ASC
        """),
        {"sid": session_id}
    )).fetchall()

    return SessionHistory(
        session_id=session_id,
        title=session[1] or "Cuộc trò chuyện mới",
        messages=[
            {
                "role": m[0],
                "content": m[1],
                "created_at": m[2].isoformat() if m[2] else "",
            }
            for m in messages
        ],
    )


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Xoá session và toàn bộ tin nhắn."""
    result = await db.execute(
        text("DELETE FROM chat_session WHERE id=:sid AND user_id=:uid RETURNING id"),
        {"sid": session_id, "uid": current_user.id}
    )
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Session không tồn tại")
    await db.commit()
    return {"deleted": True, "session_id": session_id}