"""
/api/v1/analytics — Teacher analytics: class performance, student weak spots.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc

from app.api.deps import get_current_active_user
from app.db.session import get_db
from app.db.models.user import User
from app.db.models.classroom import (
    Class, ClassMember, Assignment, Submission, AnswerDetail, StudentXP,
)
from app.db.models.question import Question

router = APIRouter()
logger = logging.getLogger(__name__)


# ─── Schemas ─────────────────────────────────────────────────

class StudentStat(BaseModel):
    student_id: int
    student_name: str
    total_submissions: int
    avg_score: Optional[float]
    last_active: Optional[datetime]
    streak_days: int
    total_xp: int
    level: int


class TopicStat(BaseModel):
    topic: str
    avg_score: float
    submission_count: int


class ClassAnalyticsSummary(BaseModel):
    class_id: int
    class_name: str
    total_students: int
    active_last_7d: int
    total_assignments: int
    avg_class_score: Optional[float]
    completion_rate: Optional[float]
    students: List[StudentStat]
    topic_breakdown: List[TopicStat]


class AssignmentAnalytics(BaseModel):
    assignment_id: int
    title: str
    total_students: int
    submitted_count: int
    completion_rate: float
    avg_score: Optional[float]
    avg_time_s: Optional[int]
    score_distribution: Dict[str, int]   # "0-20", "21-40", ...
    hardest_questions: List[Dict[str, Any]]  # question_id, text, correct_rate


# ─── Endpoints ───────────────────────────────────────────────

@router.get("/class/{class_id}", response_model=ClassAnalyticsSummary)
async def class_analytics(
    class_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Full analytics for a class — teacher only."""
    cls = await _teacher_class_or_403(class_id, current_user.id, db)

    # Members
    member_result = await db.execute(
        select(ClassMember, User)
        .join(User, ClassMember.student_id == User.id)
        .where(ClassMember.class_id == class_id, ClassMember.is_active == True)
    )
    members = member_result.all()
    total_students = len(members)

    # Active last 7 days
    since = datetime.now(timezone.utc) - timedelta(days=7)
    active_ids = await db.execute(
        select(Submission.student_id)
        .join(Assignment, Submission.assignment_id == Assignment.id)
        .where(
            Assignment.class_id == class_id,
            Submission.created_at >= since,
        )
        .distinct()
    )
    active_last_7d = len(active_ids.scalars().all())

    # Assignments
    a_count = await db.scalar(
        select(func.count()).where(Assignment.class_id == class_id)
    )

    # Avg class score
    avg_score_row = await db.scalar(
        select(func.avg(Submission.score))
        .join(Assignment, Submission.assignment_id == Assignment.id)
        .where(Assignment.class_id == class_id, Submission.status == "completed")
    )

    # Per-student stats
    student_stats: List[StudentStat] = []
    for member, user in members:
        sub_count = await db.scalar(
            select(func.count())
            .select_from(Submission)
            .join(Assignment, Submission.assignment_id == Assignment.id)
            .where(
                Assignment.class_id == class_id,
                Submission.student_id == user.id,
            )
        )
        avg_s = await db.scalar(
            select(func.avg(Submission.score))
            .join(Assignment, Submission.assignment_id == Assignment.id)
            .where(
                Assignment.class_id == class_id,
                Submission.student_id == user.id,
                Submission.status == "completed",
            )
        )
        last_sub = await db.scalar(
            select(Submission.submitted_at)
            .join(Assignment, Submission.assignment_id == Assignment.id)
            .where(
                Assignment.class_id == class_id,
                Submission.student_id == user.id,
            )
            .order_by(desc(Submission.submitted_at))
            .limit(1)
        )
        xp_rec = await db.scalar(
            select(StudentXP).where(StudentXP.student_id == user.id)
        )
        student_stats.append(StudentStat(
            student_id=user.id,
            student_name=user.full_name or user.email,
            total_submissions=sub_count or 0,
            avg_score=round(avg_s, 1) if avg_s else None,
            last_active=last_sub,
            streak_days=xp_rec.streak_days if xp_rec else 0,
            total_xp=xp_rec.total_xp if xp_rec else 0,
            level=xp_rec.level if xp_rec else 1,
        ))

    # Topic breakdown — which topics students struggle with
    topic_result = await db.execute(
        select(
            Question.topic,
            func.avg(Submission.score).label("avg_score"),
            func.count(Submission.id).label("count"),
        )
        .join(Assignment, Submission.assignment_id == Assignment.id)
        .join(AnswerDetail, AnswerDetail.submission_id == Submission.id)
        .join(Question, AnswerDetail.question_id == Question.id)
        .where(
            Assignment.class_id == class_id,
            Submission.status == "completed",
            Question.topic.isnot(None),
        )
        .group_by(Question.topic)
        .order_by(func.avg(Submission.score))
    )
    topic_breakdown = [
        TopicStat(
            topic=row.topic,
            avg_score=round(row.avg_score, 1),
            submission_count=row.count,
        )
        for row in topic_result.all()
    ]

    # Completion rate of latest assignment
    completion_rate = None
    latest_a = await db.scalar(
        select(Assignment)
        .where(Assignment.class_id == class_id, Assignment.is_active == True)
        .order_by(desc(Assignment.created_at))
        .limit(1)
    )
    if latest_a and total_students > 0:
        done = await db.scalar(
            select(func.count(Submission.student_id.distinct()))
            .where(
                Submission.assignment_id == latest_a.id,
                Submission.status == "completed",
            )
        )
        completion_rate = round((done or 0) / total_students * 100, 1)

    return ClassAnalyticsSummary(
        class_id=class_id,
        class_name=cls.name,
        total_students=total_students,
        active_last_7d=active_last_7d,
        total_assignments=a_count or 0,
        avg_class_score=round(avg_score_row, 1) if avg_score_row else None,
        completion_rate=completion_rate,
        students=sorted(student_stats, key=lambda s: -(s.avg_score or 0)),
        topic_breakdown=topic_breakdown,
    )


@router.get("/assignment/{assignment_id}", response_model=AssignmentAnalytics)
async def assignment_analytics(
    assignment_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Per-assignment analytics — teacher only."""
    assignment = await db.scalar(select(Assignment).where(Assignment.id == assignment_id))
    if not assignment:
        raise HTTPException(status_code=404, detail="Bài tập không tồn tại")

    await _teacher_class_or_403(assignment.class_id, current_user.id, db)

    # Total students in class
    total_students = await db.scalar(
        select(func.count()).where(
            ClassMember.class_id == assignment.class_id,
            ClassMember.is_active == True,
        )
    ) or 0

    # Submissions
    subs_result = await db.execute(
        select(Submission).where(
            Submission.assignment_id == assignment_id,
            Submission.status == "completed",
        )
    )
    subs = list(subs_result.scalars().all())
    submitted_count = len(subs)
    completion_rate = round(submitted_count / total_students * 100, 1) if total_students else 0.0

    avg_score = round(sum(s.score or 0 for s in subs) / submitted_count, 1) if subs else None
    avg_time  = round(sum(s.time_spent_s or 0 for s in subs) / submitted_count) if subs else None

    # Score distribution
    dist: Dict[str, int] = {"0-20": 0, "21-40": 0, "41-60": 0, "61-80": 0, "81-100": 0}
    for s in subs:
        sc = s.score or 0
        if sc <= 20:    dist["0-20"]   += 1
        elif sc <= 40:  dist["21-40"]  += 1
        elif sc <= 60:  dist["41-60"]  += 1
        elif sc <= 80:  dist["61-80"]  += 1
        else:           dist["81-100"] += 1

    # Hardest questions — lowest correct rate
    hard_result = await db.execute(
        select(
            Question.id,
            Question.question_text,
            func.count(AnswerDetail.id).label("total"),
            func.sum(AnswerDetail.is_correct.cast(int)).label("correct"),
        )
        .join(AnswerDetail, AnswerDetail.question_id == Question.id)
        .join(Submission, AnswerDetail.submission_id == Submission.id)
        .where(
            Submission.assignment_id == assignment_id,
            Submission.status == "completed",
        )
        .group_by(Question.id)
        .having(func.count(AnswerDetail.id) > 0)
        .order_by(
            (func.sum(AnswerDetail.is_correct.cast(int)) * 1.0 / func.count(AnswerDetail.id))
        )
        .limit(5)
    )
    hardest = [
        {
            "question_id": row.id,
            "question_text": row.question_text[:100],
            "correct_rate": round((row.correct or 0) / row.total * 100, 1) if row.total else 0,
            "total_attempts": row.total,
        }
        for row in hard_result.all()
    ]

    return AssignmentAnalytics(
        assignment_id=assignment_id,
        title=assignment.title,
        total_students=total_students,
        submitted_count=submitted_count,
        completion_rate=completion_rate,
        avg_score=avg_score,
        avg_time_s=avg_time,
        score_distribution=dist,
        hardest_questions=hardest,
    )


@router.get("/student/{student_id}/in-class/{class_id}")
async def student_detail_in_class(
    student_id: int,
    class_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, Any]:
    """Detailed performance of one student in a class — teacher only."""
    cls = await _teacher_class_or_403(class_id, current_user.id, db)

    student = await db.get(User, student_id)
    if not student:
        raise HTTPException(status_code=404, detail="Học sinh không tồn tại")

    result = await db.execute(
        select(Submission, Assignment)
        .join(Assignment, Submission.assignment_id == Assignment.id)
        .where(
            Assignment.class_id == class_id,
            Submission.student_id == student_id,
        )
        .order_by(Submission.submitted_at.desc())
    )
    rows = result.all()

    history = [
        {
            "assignment_id": a.id,
            "title": a.title,
            "score": s.score,
            "correct_q": s.correct_q,
            "total_q": s.total_q,
            "game_mode": s.game_mode,
            "time_spent_s": s.time_spent_s,
            "xp_earned": s.xp_earned,
            "submitted_at": s.submitted_at,
        }
        for s, a in rows
    ]

    xp_rec = await db.scalar(select(StudentXP).where(StudentXP.student_id == student_id))

    # Weak topics
    weak_result = await db.execute(
        select(
            Question.topic,
            func.avg(AnswerDetail.is_correct.cast(int)).label("rate"),
            func.count(AnswerDetail.id).label("count"),
        )
        .join(AnswerDetail, AnswerDetail.question_id == Question.id)
        .join(Submission, AnswerDetail.submission_id == Submission.id)
        .join(Assignment, Submission.assignment_id == Assignment.id)
        .where(
            Assignment.class_id == class_id,
            Submission.student_id == student_id,
            Question.topic.isnot(None),
        )
        .group_by(Question.topic)
        .order_by(func.avg(AnswerDetail.is_correct.cast(int)))
        .limit(5)
    )
    weak_topics = [
        {
            "topic": row.topic,
            "correct_rate": round((row.rate or 0) * 100, 1),
            "attempts": row.count,
        }
        for row in weak_result.all()
    ]

    return {
        "student_id": student_id,
        "student_name": student.full_name or student.email,
        "class_id": class_id,
        "class_name": cls.name,
        "total_submissions": len(history),
        "avg_score": round(sum(h["score"] or 0 for h in history) / len(history), 1) if history else None,
        "xp": {
            "total": xp_rec.total_xp if xp_rec else 0,
            "level": xp_rec.level if xp_rec else 1,
            "streak_days": xp_rec.streak_days if xp_rec else 0,
        },
        "submission_history": history,
        "weak_topics": weak_topics,
    }


# ─── Helper ──────────────────────────────────────────────────

async def _teacher_class_or_403(class_id: int, teacher_id: int, db: AsyncSession) -> Class:
    cls = await db.get(Class, class_id)
    if not cls:
        raise HTTPException(status_code=404, detail="Lớp học không tồn tại")
    if cls.teacher_id != teacher_id:
        raise HTTPException(status_code=403, detail="Không có quyền truy cập lớp này")
    return cls