"""
/api/v1/submissions — Student submits answers; teacher views class results.
Also handles XP calculation, streak, badges and leaderboard.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.api.deps import get_current_active_user
from app.db.session import get_db
from app.db.models.user import User
from app.db.models.classroom import (
    Assignment, Class, ClassMember, Submission, AnswerDetail, StudentXP, Badge,
)
from app.schemas.classroom import (
    SubmissionCreate, SubmissionResponse, StudentXPResponse, LeaderboardEntry,
)

router = APIRouter()
logger = logging.getLogger(__name__)

# XP constants
_XP_PER_CORRECT   = 20
_XP_STREAK_BONUS  = 10   # per streak day (applied to daily total)
_LEVEL_THRESHOLDS = [0, 100, 250, 500, 900, 1400, 2100, 3000, 4200, 5700, 7500]


def _calc_level(xp: int) -> int:
    for lvl, thresh in enumerate(reversed(_LEVEL_THRESHOLDS)):
        if xp >= thresh:
            return len(_LEVEL_THRESHOLDS) - lvl
    return 1


# ─── Submit ──────────────────────────────────────────────────

@router.post("", response_model=SubmissionResponse, status_code=status.HTTP_201_CREATED)
async def submit(
    payload: SubmissionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    # Verify assignment exists and student has access
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

    # XP: correct answers + streak bonus
    xp_rec = await _get_or_create_xp(current_user.id, db)
    streak_bonus = 0
    today = datetime.now(timezone.utc).date()
    if xp_rec.last_active:
        last_day = xp_rec.last_active.date()
        if last_day == today - timedelta(days=1):
            xp_rec.streak_days += 1
            streak_bonus = xp_rec.streak_days * _XP_STREAK_BONUS
        elif last_day < today - timedelta(days=1):
            xp_rec.streak_days = 1
        # same day — no change to streak
    else:
        xp_rec.streak_days = 1

    xp_earned = correct_q * _XP_PER_CORRECT + streak_bonus
    xp_rec.total_xp += xp_earned
    xp_rec.level = _calc_level(xp_rec.total_xp)
    xp_rec.last_active = datetime.now(timezone.utc)

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
        xp_earned=xp_earned,
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

    # Badges
    await _check_badges(current_user.id, sub, xp_rec, db)

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


# ─── Student: my submissions ──────────────────────────────────

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


# ─── XP + Leaderboard ────────────────────────────────────────

@router.get("/xp/me", response_model=StudentXPResponse)
async def my_xp(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    xp_rec = await _get_or_create_xp(current_user.id, db)
    await db.commit()
    return xp_rec


@router.get("/leaderboard/{class_id}", response_model=List[LeaderboardEntry])
async def leaderboard(
    class_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    # Verify access: teacher or enrolled student
    cls = await db.get(Class, class_id)
    if not cls:
        raise HTTPException(status_code=404, detail="Lớp không tồn tại")

    if cls.teacher_id != current_user.id:
        enrolled = await db.scalar(
            select(ClassMember).where(
                ClassMember.class_id == class_id,
                ClassMember.student_id == current_user.id,
                ClassMember.is_active == True,
            )
        )
        if not enrolled:
            raise HTTPException(status_code=403, detail="Không có quyền")

    result = await db.execute(
        select(StudentXP, User)
        .join(User, StudentXP.student_id == User.id)
        .join(ClassMember, ClassMember.student_id == StudentXP.student_id)
        .where(ClassMember.class_id == class_id, ClassMember.is_active == True)
        .order_by(desc(StudentXP.total_xp))
        .limit(50)
    )
    entries = []
    for rank, (xp_rec, user) in enumerate(result.all(), start=1):
        entries.append(LeaderboardEntry(
            rank=rank,
            student_id=user.id,
            student_name=user.full_name or user.email,
            total_xp=xp_rec.total_xp,
            level=xp_rec.level,
            streak_days=xp_rec.streak_days,
            is_me=(user.id == current_user.id),
        ))
    return entries


# ─── Helpers ─────────────────────────────────────────────────

async def _get_or_create_xp(student_id: int, db: AsyncSession) -> StudentXP:
    xp = await db.scalar(select(StudentXP).where(StudentXP.student_id == student_id))
    if not xp:
        xp = StudentXP(student_id=student_id)
        db.add(xp)
        await db.flush()
    return xp


async def _check_badges(
    student_id: int, sub: Submission, xp_rec: StudentXP, db: AsyncSession
) -> None:
    async def _give(badge_type: str, label: str) -> None:
        exists = await db.scalar(
            select(Badge).where(Badge.student_id == student_id, Badge.badge_type == badge_type)
        )
        if not exists:
            db.add(Badge(student_id=student_id, badge_type=badge_type, label=label))

    if sub.correct_q == sub.total_q and sub.total_q > 0:
        await _give("perfect_score", "Điểm tuyệt đối 100%")
    if xp_rec.streak_days >= 7:
        await _give("streak_7", "Học 7 ngày liên tiếp")
    if xp_rec.streak_days >= 30:
        await _give("streak_30", "Học 30 ngày liên tiếp")
    if xp_rec.total_xp >= 1000:
        await _give("xp_1000", "Đạt 1000 XP")


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
        xp_earned=sub.xp_earned,
        submitted_at=sub.submitted_at,
        created_at=sub.created_at,
    )