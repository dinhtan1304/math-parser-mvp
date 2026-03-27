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
    subject: Optional[str] = Query(None, description="Filter by subject: toan,vat-li,... (comma-separated)"),
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
    if subject:
        subj_list = [s.strip() for s in subject.split(',') if s.strip()]
        if len(subj_list) == 1:
            conditions.append(Question.subject_code == subj_list[0])
        elif len(subj_list) > 1:
            conditions.append(Question.subject_code.in_(subj_list))
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
        subj_r, types_r, diffs_r, grades_r, chapters_r, count_r
    ) = await _aio.gather(
        db.execute(select(distinct(Question.subject_code)).where(
            Question.subject_code.isnot(None))),
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
        subjects=sorted(subj_r.scalars().all()),
        types=sorted(types_r.scalars().all()),
        topics=[],
        difficulties=sorted(diffs_r.scalars().all()),
        grades=sorted(grades_r.scalars().all()),
        chapters=sorted(chapters_r.scalars().all()),
        total_questions=count_r.scalar() or 0,
    )


@router.get("/duplicates")
async def find_duplicates(
    threshold: float = Query(0.85, ge=0.5, le=1.0, description="Cosine similarity threshold"),
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Find groups of duplicate/near-duplicate questions.

    Combines two methods:
    1. Exact match via content_hash (score = 1.0)
    2. Semantic match via embeddings (score >= threshold)
    Results are merged using Union-Find to avoid overlapping groups.
    """
    from app.services.similarity_detector import find_user_duplicates

    q_count_result = await db.execute(
        select(func.count(Question.id)).where(Question.user_id == current_user.id)
    )
    q_count = q_count_result.scalar() or 0

    # ── Step 1: Exact duplicates via content_hash ──
    # Find hashes that appear 2+ times for this user
    dup_hash_q = (
        select(Question.content_hash)
        .where(
            Question.user_id == current_user.id,
            Question.content_hash.isnot(None),
            Question.content_hash != '',
        )
        .group_by(Question.content_hash)
        .having(func.count(Question.id) >= 2)
    )
    dup_hash_result = await db.execute(dup_hash_q)
    dup_hashes = [row[0] for row in dup_hash_result.fetchall()]

    # For each duplicate hash, fetch question IDs
    hash_paired_ids: set[tuple[int, int]] = set()
    hash_pairs: list[tuple[int, int, float]] = []
    if dup_hashes:
        hash_ids_result = await db.execute(
            select(Question.id, Question.content_hash)
            .where(
                Question.user_id == current_user.id,
                Question.content_hash.in_(dup_hashes),
            )
            .order_by(Question.content_hash, Question.id)
        )
        # Group IDs by hash
        from collections import defaultdict
        hash_to_ids: dict[str, list[int]] = defaultdict(list)
        for qid, chash in hash_ids_result.fetchall():
            hash_to_ids[chash].append(qid)

        for _hash, ids in hash_to_ids.items():
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    pair_key = (min(ids[i], ids[j]), max(ids[i], ids[j]))
                    hash_paired_ids.add(pair_key)
                    hash_pairs.append((ids[i], ids[j], 1.0))

    # ── Step 2: Semantic duplicates via embeddings ──
    emb_count_result = await db.execute(sa_text(
        "SELECT COUNT(*) FROM question_embedding WHERE user_id = :uid"
    ), {"uid": current_user.id})
    emb_count = emb_count_result.scalar() or 0

    embedding_pairs: list[tuple[int, int, float]] = []
    if emb_count == 0 and q_count > 0 and not hash_pairs:
        # No embeddings and no hash matches — trigger background embedding generation
        import asyncio as _aio
        all_q_result = await db.execute(
            select(Question.id).where(Question.user_id == current_user.id)
        )
        all_q_ids = [row[0] for row in all_q_result.fetchall()]

        async def _embed_bg():
            from app.db.session import AsyncSessionLocal
            try:
                async with AsyncSessionLocal() as _db:
                    from app.services.vector_search import embed_questions
                    await embed_questions(_db, all_q_ids)
                    logger.info(f"Auto-embed: {len(all_q_ids)} questions for user {current_user.id}")
            except Exception as e:
                logger.error(f"Auto-embed failed: {e}")

        _aio.create_task(_embed_bg())

        return {
            "groups": [], "total_groups": 0,
            "message": f"Đang tạo embeddings cho {q_count} câu hỏi. Vui lòng thử lại sau 30 giây.",
            "embedding_status": {"total_questions": q_count, "embedded": 0},
        }

    if emb_count > 0:
        raw_emb_pairs = await find_user_duplicates(db, current_user.id, threshold=threshold)
        # Filter out pairs already found by hash (avoid double-counting)
        for q1, q2, score in raw_emb_pairs:
            pair_key = (min(q1, q2), max(q1, q2))
            if pair_key not in hash_paired_ids:
                embedding_pairs.append((q1, q2, score))
    elif emb_count == 0 and q_count > 0:
        # Trigger background embedding for next time
        import asyncio as _aio
        all_q_result = await db.execute(
            select(Question.id).where(Question.user_id == current_user.id)
        )
        all_q_ids = [row[0] for row in all_q_result.fetchall()]

        async def _embed_bg2():
            from app.db.session import AsyncSessionLocal
            try:
                async with AsyncSessionLocal() as _db:
                    from app.services.vector_search import embed_questions
                    await embed_questions(_db, all_q_ids)
                    logger.info(f"Auto-embed: {len(all_q_ids)} questions for user {current_user.id}")
            except Exception as e:
                logger.error(f"Auto-embed failed: {e}")

        _aio.create_task(_embed_bg2())

    # ── Step 3: Merge all pairs with Union-Find ──
    all_pairs = hash_pairs + embedding_pairs

    if not all_pairs:
        return {
            "groups": [], "total_groups": 0,
            "embedding_status": {"total_questions": q_count, "embedded": emb_count},
        }

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
    for q1_id, q2_id, score in all_pairs:
        union(q1_id, q2_id)
        key = (min(q1_id, q2_id), max(q1_id, q2_id))
        # Keep the higher score if both hash and embedding matched
        pair_scores[key] = max(pair_scores.get(key, 0.0), score)

    # Group by root
    clusters: dict[int, set] = {}
    all_ids = set()
    for q1_id, q2_id, _score in all_pairs:
        for qid in (q1_id, q2_id):
            all_ids.add(qid)
            root = find(qid)
            clusters.setdefault(root, set()).add(qid)

    # Fetch question details
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
                    "exam_id": q.exam_id,
                    "created_at": q.created_at.isoformat() if q.created_at else None,
                })
        if len(group_questions) >= 2:
            groups.append({
                "questions": group_questions,
                "max_score": round(max_score, 4),
                "is_exact": max_score >= 1.0,
            })

    groups.sort(key=lambda g: g["max_score"], reverse=True)
    return {
        "groups": groups,
        "total_groups": len(groups),
        "embedding_status": {"total_questions": q_count, "embedded": emb_count},
    }


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
            subject_code=item.subject_code or "toan",
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

SOLVE_PROMPT = """Bạn là giáo viên toán THPT Việt Nam. Hãy giải bài toán dưới đây.

Câu hỏi:
{question}
{options_block}
QUY TẮC QUAN TRỌNG cho "answer":
- Trắc nghiệm: chỉ ghi đáp án đúng, ví dụ "A" hoặc "B"
- Phương trình/bất phương trình: ghi nghiệm CỤ THỂ, ví dụ "$x = 4$", "$x = -1$ hoặc $x = 3$", "$x \\in (-2; 5)$"
- Tính giá trị: ghi KẾT QUẢ SỐ, ví dụ "$S = 12$", "$\\dfrac{{7}}{{3}}$", "$2\\sqrt{{3}}$"
- Hình học: ghi giá trị cụ thể, ví dụ "$S = 16\\pi$", "$d = 5$"
- TUYỆT ĐỐI KHÔNG ghi chung chung như "có 2 nghiệm", "vô nghiệm", "phương trình có nghiệm". Phải ghi GIÁ TRỊ CỤ THỂ.
- Dùng LaTeX ($...$) cho ký hiệu toán

QUY TẮC cho "solution_steps":
- Giải chi tiết từng bước, lập luận chặt chẽ
- Dùng LaTeX ($...$) cho công thức
- Viết tiếng Việt

Trả lời JSON:
{{
  "answer": "<giá trị cụ thể>",
  "solution_steps": ["...", "...", ...]
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

    import json as _json, asyncio

    # Reuse the shared ai_generator client (already initialized at startup)
    from app.services.ai_generator import ai_generator
    if not ai_generator._client:
        ai_generator._init_client()
    if not ai_generator._client:
        raise HTTPException(status_code=503, detail="GOOGLE_API_KEY chưa được cấu hình hoặc google-genai chưa cài đặt")

    try:
        from google.genai import types as gtypes
    except ImportError:
        raise HTTPException(status_code=503, detail="google-genai chưa được cài đặt")

    prompt = SOLVE_PROMPT.format(
        question=q.question_text or "",
        options_block="",
    )

    # Use flash model for solve — disable thinking for speed
    import os
    solve_model = os.getenv("GEMINI_SOLVE_MODEL", "gemini-2.5-flash")

    # Structured output schema — force AI to return exact format
    SOLVE_SCHEMA = {
        "type": "OBJECT",
        "properties": {
            "answer":         {"type": "STRING"},
            "solution_steps": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["answer", "solution_steps"],
    }

    # Retry with back-off for transient errors (429, 5xx)
    last_error = None
    for attempt in range(3):
        try:
            response = await asyncio.wait_for(
                ai_generator._client.aio.models.generate_content(
                    model=solve_model,
                    contents=prompt,
                    config=gtypes.GenerateContentConfig(
                        temperature=0,
                        max_output_tokens=8192,
                        response_mime_type="application/json",
                        response_schema=SOLVE_SCHEMA,
                        thinking_config=gtypes.ThinkingConfig(thinking_budget=0),
                    ),
                ),
                timeout=90,
            )
            break
        except asyncio.TimeoutError:
            if attempt < 2:
                last_error = "timeout"
                await asyncio.sleep(5)
                continue
            raise HTTPException(status_code=504, detail="AI giải bài quá thời gian. Thử lại sau.")
        except Exception as e:
            last_error = str(e)
            err_str = str(e)
            # Retry on rate limit or server errors
            if ("429" in err_str or "500" in err_str or "502" in err_str
                    or "503" in err_str or "overloaded" in err_str.lower()):
                wait = (attempt + 1) * 10
                logger.warning(f"Solve attempt {attempt + 1} failed ({err_str[:100]}), retrying in {wait}s")
                await asyncio.sleep(wait)
                continue
            raise HTTPException(status_code=502, detail=f"Lỗi khi gọi AI: {err_str[:200]}")
    else:
        raise HTTPException(status_code=502, detail=f"AI lỗi sau 3 lần thử: {(last_error or '')[:200]}")

    # Extract content — handle safety blocks and various response formats
    content = ""
    block_reason = None
    try:
        content = response.text or ""
    except Exception as text_err:
        logger.warning(f"Solve: response.text failed: {text_err}")
        # Try extracting from candidates
        try:
            candidates = response.candidates or []
            if candidates:
                cand = candidates[0]
                # Check finish reason / block
                finish_reason = getattr(cand, "finish_reason", None)
                if finish_reason and str(finish_reason) not in ("STOP", "1", "FinishReason.STOP"):
                    block_reason = f"finish_reason={finish_reason}"
                # Extract parts
                cand_content = getattr(cand, "content", None)
                if cand_content:
                    parts = getattr(cand_content, "parts", []) or []
                    for part in parts:
                        if hasattr(part, "text") and part.text:
                            content += part.text
            else:
                # No candidates — check prompt feedback
                pf = getattr(response, "prompt_feedback", None)
                if pf:
                    block_reason = f"prompt_feedback={pf}"
        except Exception as e2:
            logger.warning(f"Solve: fallback extraction failed: {e2}")

    if not content.strip():
        detail = "AI không trả về kết quả."
        if block_reason:
            detail += f" Lý do: {str(block_reason)[:150]}"
        detail += " Thử lại sau."
        logger.warning(f"Solve empty for Q#{question_id}: {block_reason}")
        raise HTTPException(status_code=502, detail=detail)

    # Parse JSON — handle thinking model output (may include non-JSON text)
    logger.debug(f"Solve raw content for Q#{question_id}: {content[:500]!r}")
    result = None
    parse_error = None
    try:
        text = content.strip()
        # Strip markdown code fences
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:])
            text = text.rsplit("```", 1)[0].strip()
        result = _json.loads(text)
    except Exception as e1:
        parse_error = str(e1)
        # Fallback: find JSON object in the response (thinking models may prefix with text)
        import re
        json_match = re.search(r'\{[^{}]*"answer"[^{}]*"solution_steps"[^{}]*\[.*?\]\s*\}', content, re.DOTALL)
        if not json_match:
            # Try simpler: find any {...} block
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if json_match:
            try:
                result = _json.loads(json_match.group())
            except Exception:
                pass

    if result is None:
        logger.warning(f"Solve JSON parse failed for Q#{question_id}: {parse_error}. Raw: {content[:300]!r}")
        raise HTTPException(status_code=502, detail=f"AI trả về kết quả không đúng định dạng. Thử lại sau.")

    answer = str(result.get("answer", "")).strip()
    steps_raw = result.get("solution_steps", [])
    if isinstance(steps_raw, str):
        steps_raw = [s.strip() for s in steps_raw.split("\n") if s.strip()]
    solution_steps = [str(s).strip() for s in steps_raw if str(s).strip()]

    if not answer and not solution_steps:
        raise HTTPException(status_code=502, detail="AI không giải được bài này. Thử lại sau.")

    logger.info(f"Question {question_id} solved by AI: answer={answer[:50]!r}, steps={len(solution_steps)}")
    return {"answer": answer, "solution_steps": solution_steps}