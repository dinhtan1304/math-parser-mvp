"""
Question Bank API — list, filter, search câu hỏi trong ngân hàng.

Endpoints:
    GET /questions          — List + filter (type, topic, difficulty, keyword)
    GET /questions/filters  — Lấy danh sách filter values (để render dropdown)
    GET /questions/{id}     — Chi tiết 1 câu
    DELETE /questions/{id}  — Xóa 1 câu
"""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.db.session import get_db
from app.db.models.question import Question
from app.db.models.user import User
from app.schemas.question import QuestionResponse, QuestionListResponse, QuestionFilters

router = APIRouter()


@router.get("", response_model=QuestionListResponse)
async def list_questions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    question_type: Optional[str] = Query(None, alias="type", description="Filter by type: TN, TL, ..."),
    topic: Optional[str] = Query(None, description="Filter by topic: Đại số, Hình học, ..."),
    difficulty: Optional[str] = Query(None, description="Filter by difficulty: NB, TH, VD, VDC"),
    keyword: Optional[str] = Query(None, description="Search in question text"),
    exam_id: Optional[int] = Query(None, description="Filter by source exam"),
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List questions with optional filters."""

    # Base query: only this user's questions
    base_filter = Question.user_id == current_user.id

    # Build dynamic filters
    conditions = [base_filter]

    if question_type:
        conditions.append(Question.question_type == question_type)
    if topic:
        conditions.append(Question.topic == topic)
    if difficulty:
        conditions.append(Question.difficulty == difficulty)
    if exam_id:
        conditions.append(Question.exam_id == exam_id)
    if keyword:
        # Try FTS5 first (much faster than LIKE), fallback to LIKE
        try:
            from app.services.fts import search_fts
            fts_ids = await search_fts(db, keyword, current_user.id, limit=100)
            if fts_ids:
                conditions.append(Question.id.in_(fts_ids))
            else:
                # FTS returned nothing, use LIKE as fallback
                conditions.append(Question.question_text.ilike(f"%{keyword}%"))
        except Exception:
            conditions.append(Question.question_text.ilike(f"%{keyword}%"))

    # Count
    count_q = select(func.count(Question.id)).where(*conditions)
    total = (await db.execute(count_q)).scalar() or 0

    # Fetch page
    offset = (page - 1) * page_size
    data_q = (
        select(Question)
        .where(*conditions)
        .order_by(Question.created_at.desc(), Question.question_order)
        .offset(offset)
        .limit(page_size)
    )
    result = await db.execute(data_q)
    questions = result.scalars().all()

    return QuestionListResponse(
        items=[QuestionResponse.model_validate(q) for q in questions],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/filters", response_model=QuestionFilters)
async def get_filters(
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get available filter values for the current user's question bank.

    Returns distinct types, topics, difficulties so the frontend
    can render dropdown menus dynamically.
    """
    base = Question.user_id == current_user.id

    types_q = select(distinct(Question.question_type)).where(base, Question.question_type.isnot(None))
    topics_q = select(distinct(Question.topic)).where(base, Question.topic.isnot(None))
    diffs_q = select(distinct(Question.difficulty)).where(base, Question.difficulty.isnot(None))
    count_q = select(func.count(Question.id)).where(base)

    types = (await db.execute(types_q)).scalars().all()
    topics = (await db.execute(topics_q)).scalars().all()
    diffs = (await db.execute(diffs_q)).scalars().all()
    total = (await db.execute(count_q)).scalar() or 0

    return QuestionFilters(
        types=sorted(types),
        topics=sorted(topics),
        difficulties=sorted(diffs),
        total_questions=total,
    )


@router.get("/{question_id}", response_model=QuestionResponse)
async def get_question(
    question_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single question by ID."""
    result = await db.execute(
        select(Question).where(
            Question.id == question_id,
            Question.user_id == current_user.id,
        )
    )
    question = result.scalars().first()

    if not question:
        raise HTTPException(status_code=404, detail="Question not found")

    return question


@router.delete("/{question_id}")
async def delete_question(
    question_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a single question from the bank."""
    result = await db.execute(
        select(Question).where(
            Question.id == question_id,
            Question.user_id == current_user.id,
        )
    )
    question = result.scalars().first()

    if not question:
        raise HTTPException(status_code=404, detail="Question not found")

    await db.delete(question)

    # Cleanup FTS + vector index
    try:
        from app.services.fts import delete_fts_question
        await delete_fts_question(db, question_id)
    except Exception:
        pass
    try:
        from app.services.vector_search import delete_embedding
        await delete_embedding(db, question_id)
    except Exception:
        pass

    await db.commit()

    return {"detail": "Deleted"}