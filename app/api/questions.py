"""
Question Bank API — list, filter, search, edit, bulk-create câu hỏi.

Endpoints:
    GET    /questions          — List + filter (type, topic, difficulty, keyword)
    GET    /questions/filters  — Lấy danh sách filter values
    GET    /questions/{id}     — Chi tiết 1 câu
    PUT    /questions/{id}     — Sửa 1 câu (Sprint 2 Task 11)
    DELETE /questions/{id}     — Xóa 1 câu
    POST   /questions/bulk     — Lưu nhiều câu vào ngân hàng (Sprint 2 Task 12)
"""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.db.session import get_db
from app.db.models.question import Question
from app.db.models.user import User
from app.schemas.question import (
    QuestionResponse, QuestionListResponse, QuestionFilters,
    QuestionUpdate, QuestionBulkCreate,
)

logger = logging.getLogger(__name__)

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


# ── Sprint 2, Task 11: Edit question ──

@router.put("/{question_id}", response_model=QuestionResponse)
async def update_question(
    question_id: int,
    update: QuestionUpdate,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a question. Only provided fields are changed."""
    result = await db.execute(
        select(Question).where(
            Question.id == question_id,
            Question.user_id == current_user.id,
        )
    )
    question = result.scalars().first()

    if not question:
        raise HTTPException(status_code=404, detail="Question not found")

    # Apply only non-None fields
    update_data = update.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    for field, value in update_data.items():
        setattr(question, field, value)

    await db.commit()
    await db.refresh(question)

    # Re-sync FTS + vector index
    try:
        from app.services.fts import sync_fts_questions
        await sync_fts_questions(db, [question_id])
    except Exception:
        pass
    try:
        from app.services.vector_search import embed_questions
        await embed_questions(db, [question_id])
    except Exception:
        pass

    logger.info(f"Question {question_id} updated: {list(update_data.keys())}")
    return question


# ── Sprint 2, Task 12: Bulk save generated questions to bank ──

@router.post("/bulk", response_model=dict)
async def bulk_create_questions(
    payload: QuestionBulkCreate,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save multiple questions to the bank (e.g. from AI generator).

    Creates questions with exam_id=None (no source exam).
    Returns count of saved questions and their IDs.
    """
    if not payload.questions:
        raise HTTPException(status_code=400, detail="No questions provided")

    if len(payload.questions) > 100:
        raise HTTPException(status_code=400, detail="Maximum 100 questions per request")

    created_ids = []
    for item in payload.questions:
        q = Question(
            user_id=current_user.id,
            exam_id=None,  # No source exam — generated by AI
            question_text=item.question_text,
            question_type=item.question_type,
            topic=item.topic,
            difficulty=item.difficulty,
            answer=item.answer,
            solution_steps=item.solution_steps,
            question_order=0,
        )
        db.add(q)
        await db.flush()  # Get the ID before commit
        created_ids.append(q.id)

    await db.commit()

    # Sync FTS + vector indexes
    try:
        from app.services.fts import sync_fts_questions
        await sync_fts_questions(db, created_ids)
    except Exception as e:
        logger.debug(f"FTS sync after bulk create: {e}")

    try:
        from app.services.vector_search import embed_questions
        await embed_questions(db, created_ids)
    except Exception as e:
        logger.debug(f"Vector embed after bulk create: {e}")

    logger.info(f"Bulk created {len(created_ids)} questions for user {current_user.id}")

    return {
        "detail": f"Đã lưu {len(created_ids)} câu vào ngân hàng",
        "count": len(created_ids),
        "ids": created_ids,
    }