"""
Dashboard API — thống kê, analytics cho trang chủ.

Endpoints:
    GET /dashboard          — Tổng quan stats
    GET /dashboard/charts   — Dữ liệu cho charts
    GET /dashboard/activity — Hoạt động gần đây
"""

import json
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select, func, distinct, case, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.db.session import get_db
from app.db.models.question import Question
from app.db.models.exam import Exam
from app.db.models.user import User

router = APIRouter()


@router.get("")
async def get_dashboard(
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Dashboard overview stats."""
    uid = current_user.id

    # Total questions
    total_q = (await db.execute(
        select(func.count(Question.id)).where(Question.user_id == uid)
    )).scalar() or 0

    # Total exams
    total_exams = (await db.execute(
        select(func.count(Exam.id)).where(Exam.user_id == uid)
    )).scalar() or 0

    # Completed exams
    completed_exams = (await db.execute(
        select(func.count(Exam.id)).where(
            Exam.user_id == uid, Exam.status == "completed"
        )
    )).scalar() or 0

    # Unique topics
    topics = (await db.execute(
        select(func.count(distinct(Question.topic))).where(
            Question.user_id == uid, Question.topic.isnot(None)
        )
    )).scalar() or 0

    # Questions added this week
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    new_this_week = (await db.execute(
        select(func.count(Question.id)).where(
            Question.user_id == uid,
            Question.created_at >= week_ago
        )
    )).scalar() or 0

    # Questions added last week (for comparison)
    two_weeks_ago = week_ago - timedelta(days=7)
    last_week = (await db.execute(
        select(func.count(Question.id)).where(
            Question.user_id == uid,
            Question.created_at >= two_weeks_ago,
            Question.created_at < week_ago
        )
    )).scalar() or 0

    # Growth percentage
    if last_week > 0:
        growth = round(((new_this_week - last_week) / last_week) * 100)
    elif new_this_week > 0:
        growth = 100
    else:
        growth = 0

    return {
        "total_questions": total_q,
        "total_exams": total_exams,
        "completed_exams": completed_exams,
        "topics_count": topics,
        "new_this_week": new_this_week,
        "growth_percent": growth,
    }


@router.get("/charts")
async def get_chart_data(
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Chart data for analytics visualizations."""
    uid = current_user.id

    # By difficulty
    diff_result = await db.execute(
        select(Question.difficulty, func.count(Question.id))
        .where(Question.user_id == uid, Question.difficulty.isnot(None))
        .group_by(Question.difficulty)
    )
    by_difficulty = {row[0]: row[1] for row in diff_result.fetchall()}

    # By type
    type_result = await db.execute(
        select(Question.question_type, func.count(Question.id))
        .where(Question.user_id == uid, Question.question_type.isnot(None))
        .group_by(Question.question_type)
    )
    by_type = {row[0]: row[1] for row in type_result.fetchall()}

    # By topic (top 10)
    topic_result = await db.execute(
        select(Question.topic, func.count(Question.id).label("cnt"))
        .where(Question.user_id == uid, Question.topic.isnot(None))
        .group_by(Question.topic)
        .order_by(func.count(Question.id).desc())
        .limit(10)
    )
    by_topic = {row[0]: row[1] for row in topic_result.fetchall()}

    # Daily activity (last 30 days)
    month_ago = datetime.now(timezone.utc) - timedelta(days=30)
    daily_result = await db.execute(
        select(
            func.date(Question.created_at).label("day"),
            func.count(Question.id)
        )
        .where(Question.user_id == uid, Question.created_at >= month_ago)
        .group_by(func.date(Question.created_at))
        .order_by(text("day"))
    )
    daily_activity = {str(row[0]): row[1] for row in daily_result.fetchall()}

    return {
        "by_difficulty": by_difficulty,
        "by_type": by_type,
        "by_topic": by_topic,
        "daily_activity": daily_activity,
    }


@router.get("/activity")
async def get_recent_activity(
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Recent activity feed."""
    uid = current_user.id

    # Recent exams (last 10)
    exams_result = await db.execute(
        select(Exam)
        .where(Exam.user_id == uid)
        .order_by(Exam.created_at.desc())
        .limit(10)
    )
    exams = exams_result.scalars().all()

    activities = []
    for e in exams:
        q_count = (await db.execute(
            select(func.count(Question.id)).where(Question.exam_id == e.id)
        )).scalar() or 0

        activities.append({
            "id": e.id,
            "type": "parse",
            "filename": e.filename,
            "status": e.status,
            "question_count": q_count,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        })

    return {"activities": activities}