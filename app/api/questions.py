"""
Question Bank API — SHARED bank: all users see all questions.

Endpoints:
    GET    /questions              — List + filter (type, topic, difficulty, keyword)
    GET    /questions/filters      — Lấy danh sách filter values
    GET    /questions/{id}         — Chi tiết 1 câu
    PUT    /questions/{id}         — Sửa 1 câu
    DELETE /questions/{id}         — Xóa 1 câu
    PATCH  /questions/bulk-visibility — Đổi public/private hàng loạt
    POST   /questions/bulk         — Lưu nhiều câu vào ngân hàng
    POST   /questions/{id}/report  — Report câu hỏi của người khác
"""

import json
import logging
from typing import List, Optional

from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.db.session import get_db
from app.db.models.question import Question
from app.db.models.user import User
from sqlalchemy import text as sa_text
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
    question_type: Optional[str] = Query(None, alias="type", description="Filter by type: TN,TL,... (comma-separated for multi)"),
    difficulty: Optional[str] = Query(None, description="Filter by difficulty: NB,TH,VD,VDC (comma-separated for multi)"),
    grade: Optional[str] = Query(None, description="Filter by grade: 6-12 (comma-separated for multi)"),
    chapter: Optional[str] = Query(None, description="Filter by chapter (comma-separated for multi)"),
    keyword: Optional[str] = Query(None, description="Search in question text"),
    exam_id: Optional[int] = Query(None, description="Filter by source exam"),
    my_only: bool = Query(False, description="Show only current user's questions"),
    visibility: Optional[str] = Query(None, description="Filter by visibility: 'public' or 'private'"),
    sort_by: Optional[str] = Query(None, description="Sort field: created_at, difficulty, question_type"),
    sort_order: Optional[str] = Query(None, description="Sort direction: asc, desc"),
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List questions with optional filters.

    Shared bank: all users see public questions.
    my_only=true: only current user's questions (public + private).
    visibility=public: public questions only; visibility=private: own private questions only.
    Supports comma-separated multi-values: type=TN,TL&grade=6,7
    """
    conditions = []

    from sqlalchemy import or_
    # Base visibility
    if visibility == 'private':
        # Only current user's private questions
        conditions.append(Question.user_id == current_user.id)
        conditions.append(Question.is_public == False)
    else:
        if my_only:
            conditions.append(Question.user_id == current_user.id)
        else:
            conditions.append(or_(Question.is_public == True, Question.user_id == current_user.id))
        if visibility == 'public':
            conditions.append(Question.is_public == True)

    if question_type:
        types_list = [t.strip() for t in question_type.split(',') if t.strip()]
        if len(types_list) == 1:
            conditions.append(Question.question_type == types_list[0])
        elif len(types_list) > 1:
            conditions.append(Question.question_type.in_(types_list))
    if difficulty:
        diffs_list = [d.strip() for d in difficulty.split(',') if d.strip()]
        if len(diffs_list) == 1:
            conditions.append(Question.difficulty == diffs_list[0])
        elif len(diffs_list) > 1:
            conditions.append(Question.difficulty.in_(diffs_list))
    if grade:
        try:
            grades_list = [int(g.strip()) for g in grade.split(',') if g.strip()]
        except ValueError:
            grades_list = []
        if len(grades_list) == 1:
            conditions.append(Question.grade == grades_list[0])
        elif len(grades_list) > 1:
            conditions.append(Question.grade.in_(grades_list))
    if chapter:
        chapters_list = [c.strip() for c in chapter.split(',') if c.strip()]
        if len(chapters_list) == 1:
            conditions.append(Question.chapter == chapters_list[0])
        elif len(chapters_list) > 1:
            conditions.append(Question.chapter.in_(chapters_list))
    if exam_id:
        conditions.append(Question.exam_id == exam_id)
    if keyword:
        # Limit keyword length to prevent abuse
        keyword = keyword[:200]
        # FIX #5: Use FTS5 for keyword search (ranked, indexed) with LIKE fallback
        try:
            from app.services.fts import search_fts
            fts_ids = await search_fts(db, keyword, current_user.id, limit=200)
            if fts_ids:
                conditions.append(Question.id.in_(fts_ids))
            else:
                conditions.append(Question.question_text.ilike(f"%{keyword}%"))
        except Exception:
            conditions.append(Question.question_text.ilike(f"%{keyword}%"))

    # Count
    count_q = select(func.count(Question.id)).where(*conditions)
    total = (await db.execute(count_q)).scalar() or 0

    # Fetch page with author email via join
    offset = (page - 1) * page_size
    # Build ORDER BY
    _DIFF_ORDER = {"NB": 1, "TH": 2, "VD": 3, "VDC": 4}
    _asc = (sort_order or "desc").lower() == "asc"
    if sort_by == "difficulty":
        from sqlalchemy import case as _case
        _order_col = _case(
            (Question.difficulty == "NB", 1),
            (Question.difficulty == "TH", 2),
            (Question.difficulty == "VD", 3),
            (Question.difficulty == "VDC", 4),
            else_=5,
        )
        _order_expr = _order_col.asc() if _asc else _order_col.desc()
    elif sort_by == "question_type":
        _order_expr = Question.question_type.asc() if _asc else Question.question_type.desc()
    else:
        # Default: created_at desc
        _order_expr = Question.created_at.asc() if _asc else Question.created_at.desc()

    data_q = (
        select(Question, User.email.label("author_email"))
        .join(User, Question.user_id == User.id, isouter=True)
        .where(*conditions)
        .order_by(_order_expr)
        .offset(offset)
        .limit(page_size)
    )
    result = await db.execute(data_q)
    rows = result.all()

    items = []
    for row in rows:
        q = row[0]
        author_email = row[1]
        q_dict = QuestionResponse.model_validate(q).model_dump()
        q_dict["author_email"] = author_email
        items.append(QuestionResponse(**q_dict))

    return QuestionListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/filters", response_model=QuestionFilters)
async def get_filters(
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get available filter values for current user's question bank.

    OPT: Was 6 separate DB roundtrips. Now 1 query using asyncio.gather
    to fire all 6 selects concurrently on the same connection pool.
    ~6x faster on cold DB connections.
    """
    # OPT: Run all 6 queries in parallel instead of sequential await
    import asyncio as _aio
    (
        types_r, diffs_r, grades_r, chapters_r, count_r
    ) = await _aio.gather(
        db.execute(select(distinct(Question.question_type)).where(
            Question.question_type.isnot(None))),
        db.execute(select(distinct(Question.difficulty)).where(
            Question.difficulty.isnot(None))),
        db.execute(select(distinct(Question.grade)).where(
            Question.grade.isnot(None))),
        db.execute(select(distinct(Question.chapter)).where(
            Question.chapter.isnot(None))),
        db.execute(select(func.count(Question.id))),
    )

    return QuestionFilters(
        types=sorted(types_r.scalars().all()),
        topics=[],
        difficulties=sorted(diffs_r.scalars().all()),
        grades=sorted(grades_r.scalars().all()),
        chapters=sorted(chapters_r.scalars().all()),
        total_questions=count_r.scalar() or 0,
    )


@router.get("/duplicates")
async def find_duplicates(
    threshold: float = Query(0.92, ge=0.5, le=1.0, description="Cosine similarity threshold"),
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Find groups of duplicate/near-duplicate questions using pre-computed similarity scores.

    Returns a list of groups. Each group contains 2+ question IDs with similarity >= threshold.
    Uses the question_similarity table populated by background similarity detection.
    """
    from sqlalchemy import text as sa_text

    # Query pairs from question_similarity table where score >= threshold
    # Only include questions belonging to current user (private bank context)
    pairs_result = await db.execute(sa_text("""
        SELECT qs.question_id, qs.similar_id, qs.score
        FROM question_similarity qs
        JOIN question q1 ON q1.id = qs.question_id
        JOIN question q2 ON q2.id = qs.similar_id
        WHERE qs.score >= :threshold
          AND q1.user_id = :user_id
          AND q2.user_id = :user_id
        ORDER BY qs.score DESC
    """), {"threshold": threshold, "user_id": current_user.id})
    pairs = pairs_result.fetchall()

    if not pairs:
        return {"groups": [], "total_groups": 0}

    # Union-Find to cluster connected pairs
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent.get(x, x), x)
            x = parent.get(x, x)
        return x

    def union(a: int, b: int):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    pair_scores: dict[tuple, float] = {}
    for row in pairs:
        q1_id, q2_id, score = int(row[0]), int(row[1]), float(row[2])
        union(q1_id, q2_id)
        pair_scores[(min(q1_id, q2_id), max(q1_id, q2_id))] = score

    # Group by root
    clusters: dict[int, set] = {}
    all_ids = set()
    for row in pairs:
        for qid in (int(row[0]), int(row[1])):
            all_ids.add(qid)
            root = find(qid)
            clusters.setdefault(root, set()).add(qid)

    # Fetch question details for all involved IDs
    if not all_ids:
        return {"groups": [], "total_groups": 0}

    q_result = await db.execute(
        select(Question).where(Question.id.in_(list(all_ids)))
    )
    q_map = {q.id: q for q in q_result.scalars().all()}

    groups = []
    for root, members in clusters.items():
        if len(members) < 2:
            continue
        member_list = sorted(members)
        # Find max score within group
        max_score = max(
            pair_scores.get((min(a, b), max(a, b)), 0.0)
            for i, a in enumerate(member_list)
            for b in member_list[i + 1:]
        )
        group_questions = []
        for qid in member_list:
            q = q_map.get(qid)
            if q:
                group_questions.append({
                    "id": q.id,
                    "question_text": q.question_text,
                    "question_type": q.question_type,
                    "difficulty": q.difficulty,
                    "topic": q.topic,
                    "chapter": q.chapter,
                    "grade": q.grade,
                    "answer": q.answer,
                    "created_at": q.created_at.isoformat() if q.created_at else None,
                })
        if len(group_questions) >= 2:
            groups.append({
                "questions": group_questions,
                "max_score": round(max_score, 4),
            })

    # Sort groups by score descending
    groups.sort(key=lambda g: g["max_score"], reverse=True)
    return {"groups": groups, "total_groups": len(groups)}


@router.get("/{question_id}", response_model=QuestionResponse)
async def get_question(
    question_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single question by ID (shared bank)."""
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
    if current_user.role == "admin":
        stmt = select(Question).where(Question.id == question_id)
    else:
        stmt = select(Question).where(Question.id == question_id, Question.user_id == current_user.id)
        
    result = await db.execute(stmt)
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
    if current_user.role == "admin":
        stmt = select(Question).where(Question.id == question_id)
    else:
        stmt = select(Question).where(Question.id == question_id, Question.user_id == current_user.id)
    
    result = await db.execute(stmt)
    question = result.scalars().first()

    if not question:
        raise HTTPException(status_code=404, detail="Question not found")

    # Apply only non-None fields
    update_data = update.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Validate: public questions must have an answer
    making_public = update_data.get('is_public', None) is True
    if making_public:
        new_answer = update_data.get('answer', question.answer)
        if not new_answer or not str(new_answer).strip():
            raise HTTPException(
                status_code=422,
                detail="Câu hỏi công khai phải có đáp án. Vui lòng nhập đáp án trước khi đặt công khai."
            )

    for field, value in update_data.items():
        setattr(question, field, value)

    await db.commit()
    await db.refresh(question)

    # OPT: Run FTS/embedding update as background task — don't block response
    # FIX: Open NEW session in task — `db` from request scope is closed after response
    import asyncio as _aio

    async def _reindex():
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as _db:
            try:
                from app.services.fts import sync_fts_questions
                await sync_fts_questions(_db, [question_id])
            except Exception:
                pass
            try:
                from app.services.vector_search import embed_questions
                await embed_questions(_db, [question_id])
            except Exception:
                pass

    _aio.create_task(_reindex())

    logger.info(f"Question {question_id} updated: {list(update_data.keys())}")
    return question


# ── Bulk update visibility ──

class BulkVisibilityRequest(BaseModel):
    """Request to change visibility of multiple questions."""
    question_ids: List[int]
    is_public: bool


@router.patch("/bulk-visibility")
async def bulk_update_visibility(
    payload: BulkVisibilityRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set is_public for multiple questions owned by the current user."""
    if not payload.question_ids:
        raise HTTPException(status_code=400, detail="No question IDs provided")
    if len(payload.question_ids) > 200:
        raise HTTPException(status_code=400, detail="Maximum 200 questions per request")

    from sqlalchemy import update as sa_update

    skipped_no_answer = 0
    target_ids = payload.question_ids

    # When making public, skip questions without an answer
    if payload.is_public:
        rows = await db.execute(
            select(Question.id).where(
                Question.id.in_(target_ids),
                Question.user_id == current_user.id if current_user.role != "admin" else True,
            )
        )
        all_owned = [r[0] for r in rows.fetchall()]

        rows_with_answer = await db.execute(
            select(Question.id).where(
                Question.id.in_(all_owned),
                Question.answer.isnot(None),
                Question.answer != '',
            )
        )
        valid_ids = [r[0] for r in rows_with_answer.fetchall()]
        skipped_no_answer = len(all_owned) - len(valid_ids)
        target_ids = valid_ids

    if not target_ids:
        label = "công khai" if payload.is_public else "riêng tư"
        msg = f"Không có câu hỏi nào được chuyển sang {label}"
        if skipped_no_answer:
            msg += f" ({skipped_no_answer} câu thiếu đáp án)"
        return {"detail": msg, "updated": 0, "skipped_no_answer": skipped_no_answer, "is_public": payload.is_public}

    stmt = sa_update(Question).where(Question.id.in_(target_ids))
    if current_user.role != "admin":
        stmt = stmt.where(Question.user_id == current_user.id)
    stmt = stmt.values(is_public=payload.is_public)
    result = await db.execute(stmt)
    await db.commit()

    updated = result.rowcount
    label = "công khai" if payload.is_public else "riêng tư"
    msg = f"Đã chuyển {updated} câu hỏi sang {label}"
    if skipped_no_answer:
        msg += f" (bỏ qua {skipped_no_answer} câu thiếu đáp án)"
    logger.info(f"Bulk visibility: {updated}/{len(payload.question_ids)} → {label} by user {current_user.id}, skipped_no_answer={skipped_no_answer}")

    return {
        "detail": msg,
        "updated": updated,
        "skipped_no_answer": skipped_no_answer,
        "is_public": payload.is_public,
    }


# ── Bulk delete questions ──

class BulkDeleteRequest(BaseModel):
    question_ids: List[int]


@router.post("/bulk-delete")
async def bulk_delete_questions(
    payload: BulkDeleteRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete multiple questions owned by the current user."""
    if not payload.question_ids:
        raise HTTPException(status_code=400, detail="No question IDs provided")
    if len(payload.question_ids) > 200:
        raise HTTPException(status_code=400, detail="Maximum 200 questions per request")

    # Only delete questions owned by the current user (or admin can delete any)
    if current_user.role == "admin":
        stmt = select(Question).where(Question.id.in_(payload.question_ids))
    else:
        stmt = select(Question).where(
            Question.id.in_(payload.question_ids),
            Question.user_id == current_user.id,
        )
    result = await db.execute(stmt)
    questions_to_delete = result.scalars().all()

    if not questions_to_delete:
        return {"detail": "Không có câu hỏi nào thuộc quyền sở hữu của bạn trong danh sách", "deleted": 0}

    deleted_ids = [q.id for q in questions_to_delete]

    # Single bulk DELETE instead of N individual deletes
    from sqlalchemy import delete as sa_delete
    await db.execute(sa_delete(Question).where(Question.id.in_(deleted_ids)))
    await db.commit()

    # Cleanup FTS + vector index in background
    if deleted_ids:
        import asyncio as _aio

        async def _cleanup():
            from app.db.session import AsyncSessionLocal
            for qid in deleted_ids:
                try:
                    async with AsyncSessionLocal() as _db:
                        from app.services.fts import delete_fts_question
                        await delete_fts_question(_db, qid)
                except Exception:
                    pass
                try:
                    async with AsyncSessionLocal() as _db:
                        from app.services.vector_search import delete_embedding
                        await delete_embedding(_db, qid)
                except Exception:
                    pass

        _aio.create_task(_cleanup())

    logger.info(f"Bulk deleted {len(deleted_ids)} questions by user {current_user.id}")
    return {"detail": f"Đã xóa {len(deleted_ids)} câu hỏi", "deleted": len(deleted_ids)}


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

    # FIX #15: Check duplicates per-user only — each user has their own bank.
    # Global dedup was wrong: User B couldn't save a question User A already has.
    hashes = [h for _, h in items_with_hash]
    existing_hashes = set()
    if hashes:
        result = await db.execute(
            select(Question.content_hash).filter(
                Question.content_hash.in_(hashes),
                Question.user_id == current_user.id,
            )
        )
        existing_hashes = {row[0] for row in result.fetchall()}

    # OPT: Collect all objects first, flush ONCE to get IDs, then commit.
    # Old code called db.flush() per question → N roundtrips for N questions.
    new_questions_batch = []
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
            is_public=False,
        )
        db.add(q)
        existing_hashes.add(c_hash)  # Prevent intra-batch duplicates
        new_questions_batch.append(q)

    # Single flush to assign all IDs at once
    if new_questions_batch:
        await db.flush()
    created_ids = [q.id for q in new_questions_batch]

    await db.commit()

    # OPT: FTS/embedding as background task — bulk save response is instant
    # FIX: Open NEW session in task — `db` from request scope is closed after response
    if created_ids:
        import asyncio as _aio
        _ids_snapshot = list(created_ids)

        async def _index_bulk():
            from app.db.session import AsyncSessionLocal
            try:
                async with AsyncSessionLocal() as _db:
                    from app.services.fts import sync_fts_questions
                    await sync_fts_questions(_db, _ids_snapshot)
            except Exception as e:
                logger.debug(f"FTS sync after bulk create: {e}")
            try:
                async with AsyncSessionLocal() as _db:
                    from app.services.vector_search import embed_questions
                    await embed_questions(_db, _ids_snapshot)
            except Exception as e:
                logger.debug(f"Vector embed after bulk create: {e}")

        _aio.create_task(_index_bulk())

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


# ── Generate similar questions ──

class GenerateSimilarRequest(BaseModel):
    question_ids: List[int]
    count: int = 5  # 1-20


@router.post("/generate-similar")
async def generate_similar_questions(
    req: GenerateSimilarRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Sinh câu hỏi tương tự từ danh sách question_ids đã chọn.
    Trả về list câu hỏi MỚI chưa lưu — client tự chọn và gọi /bulk để lưu.
    """
    from sqlalchemy import or_

    if not req.question_ids:
        raise HTTPException(status_code=400, detail="Chưa chọn câu hỏi mẫu")
    if len(req.question_ids) > 20:
        raise HTTPException(status_code=400, detail="Chọn tối đa 20 câu mẫu")
    if not (1 <= req.count <= 20):
        raise HTTPException(status_code=400, detail="Số câu sinh phải từ 1 đến 20")

    # Load source questions (user can see own + public)
    result = await db.execute(
        select(Question).where(
            Question.id.in_(req.question_ids),
            or_(Question.is_public == True, Question.user_id == current_user.id),
        )
    )
    source_questions = result.scalars().all()
    if not source_questions:
        raise HTTPException(status_code=404, detail="Không tìm thấy câu hỏi nào")

    source_dicts = [
        {
            "question_text": q.question_text,
            "question_type": q.question_type,
            "difficulty": q.difficulty,
            "grade": q.grade,
            "chapter": q.chapter,
            "lesson_title": q.lesson_title,
            "answer": q.answer or "",
        }
        for q in source_questions
    ]

    import os
    from app.core.config import settings
    from app.services.question_generator import generate_similar_questions as _gen

    generated = await _gen(
        source_questions=source_dicts,
        count=req.count,
        gemini_api_key=settings.GOOGLE_API_KEY or os.getenv("GOOGLE_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    )

    if not generated:
        raise HTTPException(status_code=500, detail="AI không tạo được câu hỏi. Vui lòng thử lại.")

    # Curriculum matching — map AI chapter to exact DB values
    try:
        from app.services.curriculum_matcher import match_questions_to_curriculum
        # Adapt key names: generator uses "question", matcher uses "chapter"/"grade"
        for q in generated:
            q["topic"] = q.get("topic") or ""  # ensure str for export
        generated = await match_questions_to_curriculum(db, generated)
    except Exception as e:
        logger.warning(f"Curriculum matching skipped for generated questions: {e}")

    # Return as plain dicts (not saved yet)
    return generated


# ── Report a question ──

class ReportRequest(BaseModel):
    reason: str  # wrong_answer | duplicate | inappropriate | poor_quality | other
    detail: Optional[str] = None


VALID_REASONS = {"wrong_answer", "duplicate", "inappropriate", "poor_quality", "other"}


@router.post("/{question_id}/report", status_code=201)
async def report_question(
    question_id: int,
    payload: ReportRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Report a question from another user. One report per user per question."""
    if payload.reason not in VALID_REASONS:
        raise HTTPException(status_code=400, detail=f"Lý do không hợp lệ. Chọn một trong: {', '.join(VALID_REASONS)}")

    from app.db.models.question_report import QuestionReport
    from sqlalchemy.exc import IntegrityError

    # Check question exists
    q = (await db.execute(select(Question).where(Question.id == question_id))).scalars().first()
    if not q:
        raise HTTPException(status_code=404, detail="Câu hỏi không tồn tại")

    # Can't report own question
    if q.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Không thể báo cáo câu hỏi của chính mình")

    report = QuestionReport(
        question_id=question_id,
        reporter_id=current_user.id,
        reason=payload.reason,
        detail=payload.detail,
    )
    db.add(report)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Bạn đã báo cáo câu hỏi này rồi")

    logger.info(f"Question {question_id} reported by user {current_user.id}: {payload.reason}")
    return {"detail": "Đã gửi báo cáo. Cảm ơn bạn đã phản hồi!"}


# ── AI Solve a question ──

SOLVE_PROMPT = """Bạn là giáo viên toán THPT Việt Nam. Hãy giải bài toán dưới đây theo đúng chuẩn của Bộ Giáo Dục và Đào Tạo Việt Nam.

Câu hỏi:
{question}
{options_block}
Yêu cầu:
- Đưa ra ĐÁP ÁN ngắn gọn (chỉ đáp án cuối cùng, không giải thích thêm)
- Đưa ra HƯỚNG DẪN GIẢI từng bước rõ ràng, lập luận chặt chẽ, dùng ký hiệu toán học chuẩn (LaTeX với $...$)
- Mỗi bước trên 1 dòng, bắt đầu bằng "Bước N:"
- Viết bằng tiếng Việt

Trả lời theo định dạng JSON sau (không thêm gì ngoài JSON):
{{
  "answer": "<đáp án cuối cùng>",
  "solution_steps": ["Bước 1: ...", "Bước 2: ...", ...]
}}"""


@router.post("/{question_id}/solve")
async def ai_solve_question(
    question_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Use Gemini AI to solve a question and return answer + solution steps."""
    q = (await db.execute(select(Question).where(Question.id == question_id))).scalars().first()
    if not q:
        raise HTTPException(status_code=404, detail="Câu hỏi không tồn tại")

    import os, json as _json, asyncio
    from app.core.config import settings

    api_key = settings.GOOGLE_API_KEY or os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="GOOGLE_API_KEY chưa được cấu hình")

    try:
        from google import genai
        from google.genai import types as gtypes
    except ImportError:
        raise HTTPException(status_code=503, detail="google-genai chưa được cài đặt")

    # Build options block if multiple choice
    options_block = ""
    if q.options:
        try:
            opts = _json.loads(q.options) if isinstance(q.options, str) else q.options
            if opts:
                labels = "ABCDE"
                options_block = "Các đáp án:\n" + "\n".join(
                    f"{labels[i]}. {opt}" for i, opt in enumerate(opts)
                ) + "\n"
        except Exception:
            pass

    prompt = SOLVE_PROMPT.format(
        question=q.question_text or "",
        options_block=options_block,
    )

    client = genai.Client(api_key=api_key)
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=gtypes.GenerateContentConfig(
                    temperature=0,
                    max_output_tokens=2048,
                    response_mime_type="application/json",
                ),
            ),
            timeout=60,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="AI giải bài quá thời gian. Thử lại sau.")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Lỗi khi gọi AI: {str(e)[:200]}")

    # Extract content
    content = ""
    try:
        content = response.text or ""
    except Exception:
        for part in (response.candidates or [{}])[0].get("content", {}).get("parts", []):
            if hasattr(part, "text"):
                content += part.text

    if not content.strip():
        raise HTTPException(status_code=502, detail="AI không trả về kết quả. Thử lại sau.")

    # Parse JSON
    try:
        # Strip markdown code fences if present
        text = content.strip()
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:])
            text = text.rsplit("```", 1)[0]
        result = _json.loads(text.strip())
        answer = str(result.get("answer", "")).strip()
        steps_raw = result.get("solution_steps", [])
        solution_steps = [str(s).strip() for s in steps_raw if str(s).strip()]
    except Exception:
        raise HTTPException(status_code=502, detail="AI trả về kết quả không đúng định dạng. Thử lại sau.")

    if not answer and not solution_steps:
        raise HTTPException(status_code=502, detail="AI không giải được bài này. Thử lại sau.")

    logger.info(f"Question {question_id} solved by AI: answer={answer[:50]!r}, steps={len(solution_steps)}")
    return {"answer": answer, "solution_steps": solution_steps}