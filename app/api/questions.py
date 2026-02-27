"""
Question Bank API — SHARED bank: all users see all questions.

Endpoints:
    GET    /questions          — List + filter (type, topic, difficulty, keyword)
    GET    /questions/filters  — Lấy danh sách filter values
    GET    /questions/{id}     — Chi tiết 1 câu
    PUT    /questions/{id}     — Sửa 1 câu
    DELETE /questions/{id}     — Xóa 1 câu
    POST   /questions/bulk     — Lưu nhiều câu vào ngân hàng
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
    topic: Optional[str] = Query(None, description="Filter by topic"),
    difficulty: Optional[str] = Query(None, description="Filter by difficulty: NB, TH, VD, VDC"),
    grade: Optional[int] = Query(None, description="Filter by grade: 6-12"),
    chapter: Optional[str] = Query(None, description="Filter by chapter"),
    keyword: Optional[str] = Query(None, description="Search in question text"),
    exam_id: Optional[int] = Query(None, description="Filter by source exam"),
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List ALL questions (shared bank) with optional filters."""

    conditions = []

    if question_type:
        conditions.append(Question.question_type == question_type)
    if topic:
        conditions.append(Question.topic == topic)
    if difficulty:
        conditions.append(Question.difficulty == difficulty)
    if grade:
        conditions.append(Question.grade == grade)
    if chapter:
        conditions.append(Question.chapter == chapter)
    if exam_id:
        conditions.append(Question.exam_id == exam_id)
    if keyword:
        # FTS is user-scoped, so just use LIKE for shared bank
        conditions.append(Question.question_text.ilike(f"%{keyword}%"))

    # Count
    count_q = select(func.count(Question.id))
    if conditions:
        count_q = count_q.where(*conditions)
    total = (await db.execute(count_q)).scalar() or 0

    # Fetch page
    offset = (page - 1) * page_size
    data_q = (
        select(Question)
        .order_by(Question.created_at.desc(), Question.question_order)
        .offset(offset)
        .limit(page_size)
    )
    if conditions:
        data_q = data_q.where(*conditions)
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
    """Get available filter values for the shared question bank."""

    types_q = select(distinct(Question.question_type)).where(Question.question_type.isnot(None))
    topics_q = select(distinct(Question.topic)).where(Question.topic.isnot(None))
    diffs_q = select(distinct(Question.difficulty)).where(Question.difficulty.isnot(None))
    grades_q = select(distinct(Question.grade)).where(Question.grade.isnot(None))
    chapters_q = select(distinct(Question.chapter)).where(Question.chapter.isnot(None))
    count_q = select(func.count(Question.id))

    types = (await db.execute(types_q)).scalars().all()
    topics = (await db.execute(topics_q)).scalars().all()
    diffs = (await db.execute(diffs_q)).scalars().all()
    grades = (await db.execute(grades_q)).scalars().all()
    chapters = (await db.execute(chapters_q)).scalars().all()
    total = (await db.execute(count_q)).scalar() or 0

    return QuestionFilters(
        types=sorted(types),
        topics=sorted(topics),
        difficulties=sorted(diffs),
        grades=sorted(grades),
        chapters=sorted(chapters),
        total_questions=total,
    )


@router.get("/{question_id}", response_model=QuestionResponse)
async def get_question(
    question_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single question by ID (shared)."""
    result = await db.execute(
        select(Question).where(Question.id == question_id)
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
    """Delete a single question from the shared bank."""
    result = await db.execute(
        select(Question).where(Question.id == question_id)
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


# ── Edit question ──

@router.put("/{question_id}", response_model=QuestionResponse)
async def update_question(
    question_id: int,
    update: QuestionUpdate,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a question. Only provided fields are changed."""
    result = await db.execute(
        select(Question).where(Question.id == question_id)
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


# ── Bulk save generated questions to bank ──

@router.post("/bulk", response_model=dict)
async def bulk_create_questions(
    payload: QuestionBulkCreate,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save multiple questions to the shared bank.

    Skips duplicates via content_hash (global, not per-user).
    """
    from app.db.models.question import _question_hash

    if not payload.questions:
        raise HTTPException(status_code=400, detail="No questions provided")

    if len(payload.questions) > 100:
        raise HTTPException(status_code=400, detail="Maximum 100 questions per request")

    # Pre-compute hashes
    items_with_hash = []
    for item in payload.questions:
        if not item.question_text.strip():
            continue
        c_hash = _question_hash(item.question_text)
        items_with_hash.append((item, c_hash))

    # Check existing hashes (global — any user)
    hashes = [h for _, h in items_with_hash]
    existing_hashes = set()
    if hashes:
        result = await db.execute(
            select(Question.content_hash).filter(
                Question.content_hash.in_(hashes),
            )
        )
        existing_hashes = {row[0] for row in result.fetchall()}

    created_ids = []
    skipped = 0
    for item, c_hash in items_with_hash:
        if c_hash in existing_hashes:
            skipped += 1
            continue

        q = Question(
            user_id=current_user.id,
            exam_id=None,
            question_text=item.question_text,
            content_hash=c_hash,
            question_type=item.question_type,
            topic=item.topic,
            difficulty=item.difficulty,
            grade=item.grade,
            chapter=item.chapter,
            lesson_title=item.lesson_title,
            answer=item.answer,
            solution_steps=item.solution_steps,
            question_order=0,
        )
        db.add(q)
        await db.flush()
        created_ids.append(q.id)
        existing_hashes.add(c_hash)  # Prevent intra-batch duplicates

    await db.commit()

    # Sync FTS + vector indexes
    if created_ids:
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

    msg = f"Đã lưu {len(created_ids)} câu vào ngân hàng"
    if skipped:
        msg += f" (bỏ qua {skipped} câu trùng)"

    logger.info(f"Bulk created {len(created_ids)} questions (skipped {skipped} dupes) by user {current_user.id}")

    return {
        "detail": msg,
        "count": len(created_ids),
        "skipped": skipped,
        "ids": created_ids,
    }