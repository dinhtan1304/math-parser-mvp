"""
/api/v1/submissions — Submission records for assignments (teacher view).

NOTE (teacher-only pivot): student accounts and gamification (XP/streak/badges/
leaderboard) were removed. The submit/list endpoints are kept intact but dormant
— there are no student users to create submissions in the current product.
"""

import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.api.deps import get_current_active_user
from app.db.session import get_db
from app.db.models.user import User
from app.db.models.classroom import (
    Assignment, Class, ClassMember, Submission, AnswerDetail,
)
from app.schemas.classroom import (
    SubmissionCreate, SubmissionResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ─── Submit ──────────────────────────────────────────────────

@router.post("", response_model=SubmissionResponse, status_code=status.HTTP_201_CREATED)
async def submit(
    payload: SubmissionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    # Verify assignment exists and user has access
    assignment = await db.scalar(
        select(Assignment).where(Assignment.id == payload.assignment_id, Assignment.is_active == True)
    )
    if not assignment:
        raise HTTPException(status_code=404, detail="Bài tập không tồn tại")

    enrolled = await db.scalar(
        select(ClassMember).where(
            ClassMember.class_id == assignment.class_id,
            ClassMember.student_id == current_user.id,
            ClassMember.is_active == True,
        )
    )
    if not enrolled:
        raise HTTPException(status_code=403, detail="Bạn chưa tham gia lớp này")

    # Check attempt limit
    attempt_count = await db.scalar(
        select(func.count()).where(
            Submission.assignment_id == payload.assignment_id,
            Submission.student_id == current_user.id,
        )
    )
    if attempt_count >= assignment.max_attempts:
        raise HTTPException(
            status_code=429,
            detail=f"Bạn đã dùng hết {assignment.max_attempts} lần thử",
        )

    # Calculate score
    answers = payload.answers
    total_q  = len(answers)
    correct_q = sum(1 for a in answers if a.is_correct)
    score    = round(correct_q / total_q * 100) if total_q else 0

    # Save submission
    sub = Submission(
        assignment_id=payload.assignment_id,
        student_id=current_user.id,
        score=score,
        total_q=total_q,
        correct_q=correct_q,
        time_spent_s=payload.time_spent_s,
        attempt_no=(attempt_count or 0) + 1,
        game_mode=payload.game_mode,
        status="completed",
        submitted_at=datetime.now(timezone.utc),
    )
    db.add(sub)
    await db.flush()

    # Save answer details
    for a in answers:
        db.add(AnswerDetail(
            submission_id=sub.id,
            question_id=a.question_id,
            given_answer=a.given_answer,
            is_correct=a.is_correct,
            time_ms=a.time_ms,
        ))

    await db.commit()
    await db.refresh(sub)
    return _sub_response(sub, current_user.full_name)


# ─── Teacher: view submissions for assignment ─────────────────

@router.get("/assignment/{assignment_id}", response_model=List[SubmissionResponse])
async def list_submissions(
    assignment_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    # Verify teacher owns the assignment
    assignment = await db.scalar(select(Assignment).where(Assignment.id == assignment_id))
    if not assignment:
        raise HTTPException(status_code=404, detail="Bài tập không tồn tại")
    cls = await db.get(Class, assignment.class_id)
    if not cls or cls.teacher_id != current_user.id:
        raise HTTPException(status_code=403, detail="Không có quyền")

    result = await db.execute(
        select(Submission, User)
        .join(User, Submission.student_id == User.id)
        .where(Submission.assignment_id == assignment_id)
        .order_by(desc(Submission.score), Submission.time_spent_s)
    )
    return [_sub_response(s, u.full_name) for s, u in result.all()]


# ─── My submissions ───────────────────────────────────────────

@router.get("/my", response_model=List[SubmissionResponse])
async def my_submissions(
    assignment_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    q = select(Submission).where(Submission.student_id == current_user.id)
    if assignment_id:
        q = q.where(Submission.assignment_id == assignment_id)
    q = q.order_by(Submission.created_at.desc())
    result = await db.execute(q)
    return [_sub_response(s, current_user.full_name) for s in result.scalars().all()]


# ─── Helpers ─────────────────────────────────────────────────

def _sub_response(sub: Submission, student_name: str | None) -> SubmissionResponse:
    return SubmissionResponse(
        id=sub.id,
        assignment_id=sub.assignment_id,
        student_id=sub.student_id,
        student_name=student_name,
        score=sub.score,
        total_q=sub.total_q,
        correct_q=sub.correct_q,
        time_spent_s=sub.time_spent_s,
        attempt_no=sub.attempt_no,
        game_mode=sub.game_mode,
        status=sub.status,
        submitted_at=sub.submitted_at,
        created_at=sub.created_at,
    )
