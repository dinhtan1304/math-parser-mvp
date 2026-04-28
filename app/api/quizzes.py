"""
Quiz API — CRUD, import questions, manage theories, publish.
"""

import logging
from typing import Dict, Optional, Tuple
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, or_
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_active_user, get_optional_user
from app.db.session import get_db
from app.db.models.user import User
from app.db.models.question import Question
from app.db.models.quiz import Quiz, QuizTheory, QuizTheorySection, QuizQuestion
from app.schemas.quiz import (
    QuizCreate, QuizUpdate, QuizResponse, QuizListResponse,
    QuizTheoryCreate, QuizTheoryUpdate, QuizTheoryResponse,
    QuizQuestionCreate, QuizQuestionUpdate, QuizQuestionResponse,
    BatchCreateQuestionsRequest,
    ImportQuestionsRequest, ImportQuestionsResponse, SkippedQuestion,
    QuizDeliveryResponse, QuizDeliveryQuestion,
    TheorySectionResponse,
)
from app.services.quiz_builder import convert_bank_questions
from app.services.quiz_bank_sync import save_to_bank, save_to_bank_batch, _content_hash

logger = logging.getLogger(__name__)
router = APIRouter()


def _now():
    return datetime.now(timezone.utc)


def _escape_like(value: str) -> str:
    """Escape special LIKE metacharacters to prevent pattern injection."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ─── Quiz CRUD ───────────────────────────────────────────────

@router.post("/", response_model=QuizResponse, status_code=201)
async def create_quiz(
    data: QuizCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    quiz = Quiz(
        name=data.name,
        description=data.description,
        cover_image_url=data.cover_image_url,
        created_by_id=user.id,
        subject_code=data.subject_code,
        grade=data.grade,
        mode=data.mode,
        language=data.language,
        visibility=data.visibility,
        tags=data.tags,
        settings=data.settings.model_dump() if data.settings else {},
    )
    db.add(quiz)
    await db.commit()
    await db.refresh(quiz)
    return quiz


@router.get("/", response_model=QuizListResponse)
async def list_quizzes(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    query = select(Quiz).where(Quiz.created_by_id == user.id)
    count_query = select(func.count(Quiz.id)).where(Quiz.created_by_id == user.id)

    if status:
        query = query.where(Quiz.status == status)
        count_query = count_query.where(Quiz.status == status)

    if search:
        safe_search = _escape_like(search)
        query = query.where(Quiz.name.ilike(f"%{safe_search}%"))
        count_query = count_query.where(Quiz.name.ilike(f"%{safe_search}%"))

    total = (await db.execute(count_query)).scalar() or 0
    query = query.order_by(desc(Quiz.created_at)).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    items = result.scalars().all()

    return QuizListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/by-code/{code}", response_model=QuizDeliveryResponse)
async def get_quiz_by_code(
    code: str,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Lookup a published quiz by its code — no auth required for published quizzes."""
    clean = code.upper().strip()
    if not clean.startswith("QUIZ-"):
        clean = f"QUIZ-{clean}"
    result = await db.execute(select(Quiz).where(Quiz.code == clean))
    quiz = result.scalars().first()
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz không tồn tại")
    if quiz.status != "published":
        if not user or quiz.created_by_id != user.id:
            raise HTTPException(status_code=403, detail="Quiz chưa được xuất bản")

    return await _build_delivery_response(db, quiz)


@router.get("/{quiz_id}", response_model=QuizResponse)
async def get_quiz(
    quiz_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    quiz = await _get_quiz_owned(db, quiz_id, user.id)
    return quiz


@router.patch("/{quiz_id}", response_model=QuizResponse)
async def update_quiz(
    quiz_id: int,
    data: QuizUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    quiz = await _get_quiz_owned(db, quiz_id, user.id)

    update_fields = data.model_dump(exclude_unset=True)
    if "settings" in update_fields and data.settings:
        update_fields["settings"] = data.settings.model_dump()

    if "status" in update_fields and update_fields["status"] == "published" and quiz.status != "published":
        update_fields["published_at"] = _now()

    for key, value in update_fields.items():
        setattr(quiz, key, value)

    quiz.updated_at = _now()
    await db.commit()
    await db.refresh(quiz)
    return quiz


@router.get("/{quiz_id}/delete-info")
async def get_quiz_delete_info(
    quiz_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Get counts of related data before deleting a quiz."""
    from app.db.models.quiz_attempt import QuizAttempt
    from app.db.models.classroom import Assignment

    quiz = await _get_quiz_owned(db, quiz_id, user.id)

    theory_count = (await db.execute(
        select(func.count(QuizTheory.id)).where(QuizTheory.quiz_id == quiz_id)
    )).scalar() or 0
    attempt_count = (await db.execute(
        select(func.count(QuizAttempt.id)).where(QuizAttempt.quiz_id == quiz_id)
    )).scalar() or 0
    assignment_count = (await db.execute(
        select(func.count(Assignment.id)).where(Assignment.quiz_id == quiz_id)
    )).scalar() or 0

    return {
        "quiz_name": quiz.name,
        "question_count": quiz.question_count,
        "theory_count": theory_count,
        "attempt_count": attempt_count,
        "assignment_count": assignment_count,
    }


@router.delete("/{quiz_id}", status_code=204)
async def delete_quiz(
    quiz_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    quiz = await _get_quiz_owned(db, quiz_id, user.id)
    await db.delete(quiz)
    await db.commit()


# ─── Quiz Questions ──────────────────────────────────────────

@router.post("/{quiz_id}/questions", response_model=QuizQuestionResponse, status_code=201)
async def add_question(
    quiz_id: int,
    data: QuizQuestionCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    quiz = await _get_quiz_owned(db, quiz_id, user.id)

    # Auto-assign order if not provided
    order = data.order
    if order is None:
        order = quiz.question_count

    # Save to bank for reuse (dedup by content_hash)
    bank_q = await save_to_bank(db, user.id, data, grade=quiz.grade)

    qq = _build_quiz_question(
        quiz_id=quiz.id,
        q_data=data,
        order=order,
        source_type="manual",
        origin_id=bank_q.id,
    )
    db.add(qq)

    # Update denormalized counts
    quiz.question_count += 1
    quiz.total_points = float(quiz.total_points or 0) + float(data.points)
    quiz.updated_at = _now()

    await db.commit()
    await db.refresh(qq)
    return qq


@router.post("/{quiz_id}/batch-questions", response_model=list[QuizQuestionResponse], status_code=201)
async def batch_create_questions(
    quiz_id: int,
    data: BatchCreateQuestionsRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Batch-create questions (e.g. from JSON file import)."""
    quiz = await _get_quiz_owned(db, quiz_id, user.id)

    try:
        # Batch save to bank: 1 SELECT + 1 bulk INSERT (instead of N+1)
        bank_map = await save_to_bank_batch(db, user.id, data.questions, grade=quiz.grade)

        # Load existing question hashes for duplicate detection
        existing_result = await db.execute(
            select(QuizQuestion.question_text).where(QuizQuestion.quiz_id == quiz.id)
        )
        existing_hashes = {_content_hash(t) for t in existing_result.scalars().all()}

        created = []
        skipped_dupes = 0
        base_order = quiz.question_count
        total_pts = 0.0

        for i, q_data in enumerate(data.questions):
            h = _content_hash(q_data.question_text)
            if h in existing_hashes:
                skipped_dupes += 1
                continue
            existing_hashes.add(h)

            # Always auto-assign sequential order (ignore order from JSON to avoid gaps)
            order = base_order + len(created)
            bank_q = bank_map.get(h)

            qq = _build_quiz_question(
                quiz_id=quiz.id,
                q_data=q_data,
                order=order,
                source_type=data.source_type,
                origin_id=bank_q.id if bank_q else None,
            )
            db.add(qq)
            created.append(qq)
            total_pts += float(q_data.points)

        if skipped_dupes > 0:
            logger.info(f"Batch import: skipped {skipped_dupes} duplicate(s) in quiz {quiz_id}")

        quiz.question_count += len(created)
        quiz.total_points = float(quiz.total_points or 0) + total_pts
        quiz.updated_at = _now()

        await db.commit()
        for qq in created:
            await db.refresh(qq)

        # Return with skip info in headers so FE can report
        from fastapi.responses import JSONResponse as _JR
        from app.schemas.quiz import QuizQuestionResponse as _QR
        body = [_QR.model_validate(qq).model_dump(mode="json") for qq in created]
        return _JR(
            content=body,
            status_code=201,
            headers={"X-Skipped-Duplicates": str(skipped_dupes)},
        )
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Batch import failed for quiz {quiz_id}: {e}")
        raise HTTPException(status_code=400, detail=f"Batch import failed: {str(e)}")


@router.get("/{quiz_id}/questions", response_model=list[QuizQuestionResponse])
async def list_questions(
    quiz_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    await _get_quiz_owned(db, quiz_id, user.id)
    result = await db.execute(
        select(QuizQuestion)
        .where(QuizQuestion.quiz_id == quiz_id)
        .order_by(QuizQuestion.order)
    )
    return result.scalars().all()


@router.patch("/{quiz_id}/questions/{question_id}", response_model=QuizQuestionResponse)
async def update_question(
    quiz_id: int,
    question_id: int,
    data: QuizQuestionUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    quiz = await _get_quiz_owned(db, quiz_id, user.id)
    qq = await _get_quiz_question(db, quiz_id, question_id)

    old_points = float(qq.points)
    update_fields = data.model_dump(exclude_unset=True)

    # Convert nested Pydantic models to dicts
    if "choices" in update_fields and data.choices is not None:
        update_fields["choices"] = [c.model_dump() for c in data.choices]
    if "items" in update_fields and data.items is not None:
        update_fields["items"] = [i.model_dump() for i in data.items]
    if "scoring" in update_fields and data.scoring is not None:
        update_fields["scoring"] = data.scoring.model_dump()
    if "solution" in update_fields and data.solution is not None:
        update_fields["solution"] = data.solution.model_dump()
    if "metadata" in update_fields:
        update_fields["extra_metadata"] = update_fields.pop("metadata")

    for key, value in update_fields.items():
        setattr(qq, key, value)

    qq.updated_at = _now()

    # Update denormalized total_points
    new_points = float(qq.points)
    if new_points != old_points:
        quiz.total_points = float(quiz.total_points or 0) - old_points + new_points
        quiz.updated_at = _now()

    await db.commit()
    await db.refresh(qq)
    return qq


@router.delete("/{quiz_id}/questions/{question_id}", status_code=204)
async def delete_question(
    quiz_id: int,
    question_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    quiz = await _get_quiz_owned(db, quiz_id, user.id)
    qq = await _get_quiz_question(db, quiz_id, question_id)

    quiz.question_count = max(0, quiz.question_count - 1)
    quiz.total_points = max(0, float(quiz.total_points or 0) - float(qq.points))
    quiz.updated_at = _now()

    await db.delete(qq)
    await db.commit()


@router.post("/{quiz_id}/reconcile", response_model=QuizResponse)
async def reconcile_quiz(
    quiz_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Recompute denormalized question_count and total_points."""
    quiz = await _get_quiz_owned(db, quiz_id, user.id)
    await _reconcile_quiz_counts(db, quiz)
    quiz.updated_at = _now()
    await db.commit()
    await db.refresh(quiz)
    return quiz


# ─── Import from Bank ────────────────────────────────────────

@router.post("/{quiz_id}/import-questions", response_model=ImportQuestionsResponse)
async def import_questions_from_bank(
    quiz_id: int,
    data: ImportQuestionsRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    quiz = await _get_quiz_owned(db, quiz_id, user.id)
    total_requested = len(data.question_ids)
    skipped_items: list[SkippedQuestion] = []

    # Fetch bank questions — only own or public
    result = await db.execute(
        select(Question).where(
            Question.id.in_(data.question_ids),
            or_(Question.user_id == user.id, Question.is_public == True),
        )
    )
    bank_questions = result.scalars().all()
    found_ids = {bq.id for bq in bank_questions}

    # Track questions skipped due to no access
    for qid in data.question_ids:
        if qid not in found_ids:
            skipped_items.append(SkippedQuestion(question_id=qid, reason="no_access"))

    if not bank_questions and not skipped_items:
        raise HTTPException(status_code=404, detail="No matching bank questions found")

    # Filter out questions with empty text
    valid_bank_questions = []
    for bq in bank_questions:
        if not (bq.question_text or "").strip():
            skipped_items.append(SkippedQuestion(question_id=bq.id, reason="empty_text"))
        else:
            valid_bank_questions.append(bq)

    # Convert bank → quiz questions (async — may call LLM when target_type is set)
    start_order = quiz.question_count
    converted, convert_errors = await convert_bank_questions(
        valid_bank_questions,
        source_type=data.source_type,
        start_order=start_order,
        target_type=data.target_type,
    )

    # Track questions that failed during conversion
    for bq_id, error_msg in convert_errors:
        skipped_items.append(SkippedQuestion(question_id=bq_id, reason="convert_error"))
        logger.warning(f"Bank import: convert failed for question {bq_id}: {error_msg}")

    created = []
    total_new_points = 0.0
    for conv in converted:
        qq = QuizQuestion(quiz_id=quiz.id, **conv)
        db.add(qq)
        created.append(qq)
        total_new_points += float(conv.get("points", 1.0))

    # Update denormalized counts
    if created:
        quiz.question_count += len(created)
        quiz.total_points = float(quiz.total_points or 0) + total_new_points
        quiz.updated_at = _now()

    await db.commit()
    for qq in created:
        await db.refresh(qq)

    if skipped_items:
        logger.info(
            f"Bank import: {len(created)}/{total_requested} imported, "
            f"{len(skipped_items)} skipped for user {user.id}"
        )

    return ImportQuestionsResponse(
        imported=created,
        imported_count=len(created),
        skipped=skipped_items,
        skipped_count=len(skipped_items),
        total_requested=total_requested,
    )


# ─── Quiz Theories ───────────────────────────────────────────

@router.post("/{quiz_id}/theories", response_model=QuizTheoryResponse, status_code=201)
async def add_theory(
    quiz_id: int,
    data: QuizTheoryCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    await _get_quiz_owned(db, quiz_id, user.id)

    theory = QuizTheory(
        quiz_id=quiz_id,
        title=data.title,
        content_type=data.content_type,
        language=data.language,
        tags=data.tags,
        display_order=data.display_order,
    )
    db.add(theory)
    await db.flush()  # get theory.id

    for s in data.sections:
        section = QuizTheorySection(
            theory_id=theory.id,
            order=s.order,
            content=s.content,
            content_format=s.content_format,
            media=s.media,
        )
        db.add(section)

    await db.commit()

    # Reload with sections
    result = await db.execute(
        select(QuizTheory)
        .options(selectinload(QuizTheory.sections))
        .where(QuizTheory.id == theory.id)
    )
    return result.scalars().first()


@router.post("/{quiz_id}/batch-theories", response_model=list[QuizTheoryResponse], status_code=201)
async def batch_create_theories(
    quiz_id: int,
    theories: list[QuizTheoryCreate] = Body(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Batch-create theories (used during quiz import)."""
    await _get_quiz_owned(db, quiz_id, user.id)

    created_ids = []
    for t_data in theories:
        theory = QuizTheory(
            quiz_id=quiz_id,
            title=t_data.title,
            content_type=t_data.content_type,
            language=t_data.language,
            tags=t_data.tags,
            display_order=t_data.display_order,
        )
        db.add(theory)
        await db.flush()

        for s in t_data.sections:
            section = QuizTheorySection(
                theory_id=theory.id,
                order=s.order,
                content=s.content,
                content_format=s.content_format,
                media=s.media,
            )
            db.add(section)
        created_ids.append(theory.id)

    await db.commit()

    result = await db.execute(
        select(QuizTheory)
        .options(selectinload(QuizTheory.sections))
        .where(QuizTheory.id.in_(created_ids))
        .order_by(QuizTheory.display_order)
    )
    return result.scalars().all()


@router.get("/{quiz_id}/theories", response_model=list[QuizTheoryResponse])
async def list_theories(
    quiz_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    await _get_quiz_owned(db, quiz_id, user.id)
    result = await db.execute(
        select(QuizTheory)
        .options(selectinload(QuizTheory.sections))
        .where(QuizTheory.quiz_id == quiz_id)
        .order_by(QuizTheory.display_order)
    )
    return result.scalars().all()


@router.delete("/{quiz_id}/theories/{theory_id}", status_code=204)
async def delete_theory(
    quiz_id: int,
    theory_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    await _get_quiz_owned(db, quiz_id, user.id)
    result = await db.execute(
        select(QuizTheory).where(QuizTheory.id == theory_id, QuizTheory.quiz_id == quiz_id)
    )
    theory = result.scalars().first()
    if not theory:
        raise HTTPException(status_code=404, detail="Theory not found")
    await db.delete(theory)
    await db.commit()


# ─── Quiz Delivery (for students) ────────────────────────────

async def _build_delivery_response(db: AsyncSession, quiz: Quiz) -> QuizDeliveryResponse:
    """Shared helper: load questions + theories, strip answers for delivery."""
    q_result = await db.execute(
        select(QuizQuestion)
        .where(QuizQuestion.quiz_id == quiz.id)
        .order_by(QuizQuestion.order)
    )
    questions = q_result.scalars().all()

    t_result = await db.execute(
        select(QuizTheory)
        .options(selectinload(QuizTheory.sections))
        .where(QuizTheory.quiz_id == quiz.id)
        .order_by(QuizTheory.display_order)
    )
    theories = t_result.scalars().all()

    delivery_questions = []
    for q in questions:
        safe_choices = None
        if q.choices:
            safe_choices = [
                {"key": c["key"], "text": c["text"], "media": c.get("media")}
                for c in q.choices
            ]
        # For fill_blank: compute blank count from answer keys
        blank_count = 0
        blank_labels = None
        if q.type == "fill_blank" and isinstance(q.answer, dict):
            labels = sorted(q.answer.keys())
            blank_count = len(labels)
            blank_labels = labels

        delivery_questions.append(QuizDeliveryQuestion(
            id=q.id, order=q.order, code=q.code, type=q.type,
            question_text=q.question_text, required=q.required,
            points=float(q.points), time_limit_seconds=q.time_limit_seconds,
            difficulty=q.difficulty, media=q.media, choices=safe_choices,
            items=q.items, blank_count=blank_count, blank_labels=blank_labels,
            has_hint=q.hint_section_id is not None,
            hint_section_id=q.hint_section_id,
        ))

    return QuizDeliveryResponse(
        id=quiz.id, code=quiz.code, name=quiz.name,
        description=quiz.description, cover_image_url=quiz.cover_image_url,
        subject_code=quiz.subject_code, grade=quiz.grade, mode=quiz.mode,
        settings=quiz.settings or {},
        question_count=quiz.question_count,
        total_points=float(quiz.total_points or 0),
        questions=delivery_questions, theories=theories,
    )


@router.get("/{quiz_id}/deliver", response_model=QuizDeliveryResponse)
async def deliver_quiz(
    quiz_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Get quiz for student to take — strips answers/solutions."""
    result = await db.execute(select(Quiz).where(Quiz.id == quiz_id))
    quiz = result.scalars().first()
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found")
    if quiz.status != "published" and quiz.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="Quiz not available")
    return await _build_delivery_response(db, quiz)


# ─── Export ─────────────────────────────────────────────────

@router.get("/{quiz_id}/export")
async def export_quiz(
    quiz_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
):
    """Export quiz as JSON (matching import format for round-trip)."""
    from fastapi.responses import JSONResponse

    quiz = await _get_quiz_owned(db, quiz_id, user.id)

    # Load questions
    q_result = await db.execute(
        select(QuizQuestion)
        .where(QuizQuestion.quiz_id == quiz_id)
        .order_by(QuizQuestion.order)
    )
    questions = q_result.scalars().all()

    # Load theories with sections
    t_result = await db.execute(
        select(QuizTheory)
        .options(selectinload(QuizTheory.sections))
        .where(QuizTheory.quiz_id == quiz_id)
        .order_by(QuizTheory.display_order)
    )
    theories = t_result.scalars().all()

    # Build section_id → (theory_index, section_index) map for hint round-trip
    section_id_map: Dict[int, Tuple[int, int]] = {}
    for ti, t in enumerate(theories):
        for si, s in enumerate(sorted(t.sections, key=lambda x: x.order)):
            section_id_map[s.id] = (ti, si)

    def _question_hint(q: QuizQuestion) -> dict:
        """Resolve hint_section_id to portable theory_index + section_index."""
        if q.hint_section_id and q.hint_section_id in section_id_map:
            ti, si = section_id_map[q.hint_section_id]
            return {"hint_theory_index": ti, "hint_section_index": si}
        return {}

    export_data = {
        "quiz": {
            "name": quiz.name,
            "description": quiz.description,
            "cover_image_url": quiz.cover_image_url,
            "subject_code": quiz.subject_code,
            "grade": quiz.grade,
            "mode": quiz.mode,
            "language": quiz.language,
            "visibility": quiz.visibility,
            "tags": quiz.tags or [],
            "settings": quiz.settings or {},
        },
        "theories": [
            {
                "title": t.title,
                "content_type": t.content_type,
                "language": t.language,
                "tags": t.tags or [],
                "display_order": t.display_order,
                "sections": [
                    {
                        "order": s.order,
                        "content": s.content,
                        "content_format": s.content_format,
                        "media": s.media,
                    }
                    for s in sorted(t.sections, key=lambda x: x.order)
                ],
            }
            for t in theories
        ],
        "questions": [
            {
                "type": q.type,
                "question_text": q.question_text,
                "order": q.order,
                "code": q.code,
                "has_correct_answer": q.has_correct_answer,
                "required": q.required,
                "points": float(q.points),
                "time_limit_seconds": q.time_limit_seconds,
                "difficulty": q.difficulty,
                "subject_code": q.subject_code,
                "tags": q.tags or [],
                "media": q.media,
                "answer": q.answer,
                "choices": q.choices,
                "items": q.items,
                "scoring": q.scoring,
                "solution": q.solution,
                **_question_hint(q),
            }
            for q in questions
        ],
    }

    safe_name = quiz.code or f"quiz-{quiz.id}"
    return JSONResponse(
        content=export_data,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.json"'},
    )


# ─── Helpers ─────────────────────────────────────────────────

def _build_quiz_question(
    quiz_id: int,
    q_data: QuizQuestionCreate,
    order: int,
    source_type: str,
    origin_id: Optional[int] = None,
) -> QuizQuestion:
    """Create a QuizQuestion ORM object from schema data."""
    return QuizQuestion(
        quiz_id=quiz_id,
        source_type=source_type,
        origin_question_id=origin_id,
        order=order,
        code=q_data.code,
        type=q_data.type,
        question_text=q_data.question_text,
        has_correct_answer=q_data.has_correct_answer,
        required=q_data.required,
        points=q_data.points,
        time_limit_seconds=q_data.time_limit_seconds,
        difficulty=q_data.difficulty,
        subject_code=q_data.subject_code,
        tags=q_data.tags,
        media=q_data.media,
        answer=q_data.answer,
        choices=[c.model_dump() for c in q_data.choices] if q_data.choices else None,
        items=[i.model_dump() for i in q_data.items] if q_data.items else None,
        scoring=q_data.scoring.model_dump() if q_data.scoring else None,
        solution=q_data.solution.model_dump() if q_data.solution else None,
        hint_section_id=q_data.hint_section_id,
        hint_auto_linked=q_data.hint_auto_linked,
        extra_metadata=q_data.metadata,
    )


async def _get_quiz_owned(db: AsyncSession, quiz_id: int, user_id: int) -> Quiz:
    result = await db.execute(select(Quiz).where(Quiz.id == quiz_id))
    quiz = result.scalars().first()
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found")
    if quiz.created_by_id != user_id:
        raise HTTPException(status_code=403, detail="Not your quiz")
    return quiz


async def _get_quiz_question(db: AsyncSession, quiz_id: int, question_id: int) -> QuizQuestion:
    result = await db.execute(
        select(QuizQuestion).where(
            QuizQuestion.id == question_id,
            QuizQuestion.quiz_id == quiz_id,
        )
    )
    qq = result.scalars().first()
    if not qq:
        raise HTTPException(status_code=404, detail="Question not found in this quiz")
    return qq


async def _reconcile_quiz_counts(db: AsyncSession, quiz: Quiz) -> None:
    """Recompute denormalized question_count and total_points from actual data."""
    result = await db.execute(
        select(
            func.count(QuizQuestion.id),
            func.coalesce(func.sum(QuizQuestion.points), 0),
        ).where(QuizQuestion.quiz_id == quiz.id)
    )
    count, total = result.first()
    quiz.question_count = count
    quiz.total_points = float(total)
