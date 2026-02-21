"""
Dashboard API — thống kê, analytics cho trang chủ.

Endpoints:
    GET /dashboard          — Tổng quan stats
    GET /dashboard/charts   — Dữ liệu cho charts
    GET /dashboard/activity — Hoạt động gần đây

Optimizations (Sprint 1):
    - Task 3: Activity N+1 → single JOIN query (11 queries → 1)
    - Task 4: Stats 6 queries → 2 queries (conditional aggregation)
"""

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
    """
    Dashboard overview stats.

    BEFORE (6 queries):
        SELECT COUNT(*) FROM question WHERE user_id=?      -- total_q
        SELECT COUNT(*) FROM exam WHERE user_id=?           -- total_exams
        SELECT COUNT(*) FROM exam WHERE user_id=? AND ...   -- completed_exams
        SELECT COUNT(DISTINCT topic) FROM question ...      -- topics
        SELECT COUNT(*) FROM question WHERE created_at>=?   -- new_this_week
        SELECT COUNT(*) FROM question WHERE created_at ...  -- last_week

    AFTER (2 queries):
        Query 1: question stats (total, topics, this_week, last_week)
        Query 2: exam stats (total, completed)
    """
    uid = current_user.id
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    two_weeks_ago = week_ago - timedelta(days=7)

    # ── Query 1: All question stats in ONE query ──
    q_stats = (await db.execute(
        select(
            func.count(Question.id).label("total"),
            func.count(distinct(
                case((Question.topic.isnot(None), Question.topic), else_=None)
            )).label("topics"),
            func.sum(
                case((Question.created_at >= week_ago, 1), else_=0)
            ).label("new_this_week"),
            func.sum(
                case(
                    (
                        (Question.created_at >= two_weeks_ago)
                        & (Question.created_at < week_ago),
                        1,
                    ),
                    else_=0,
                )
            ).label("last_week"),
        ).where(Question.user_id == uid)
    )).one()

    total_q = q_stats.total or 0
    topics = q_stats.topics or 0
    new_this_week = int(q_stats.new_this_week or 0)
    last_week = int(q_stats.last_week or 0)

    # ── Query 2: All exam stats in ONE query ──
    e_stats = (await db.execute(
        select(
            func.count(Exam.id).label("total"),
            func.sum(
                case((Exam.status == "completed", 1), else_=0)
            ).label("completed"),
        ).where(Exam.user_id == uid)
    )).one()

    total_exams = e_stats.total or 0
    completed_exams = int(e_stats.completed or 0)

    # ── Growth calculation ──
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
    """
    Recent activity feed.

    BEFORE (N+1 — 11 queries for 10 exams):
        SELECT * FROM exam WHERE user_id=? LIMIT 10          -- 1 query
        SELECT COUNT(*) FROM question WHERE exam_id=1         -- query 2
        SELECT COUNT(*) FROM question WHERE exam_id=2         -- query 3
        ...                                                    -- query 11

    AFTER (1 query with LEFT JOIN + GROUP BY):
        SELECT exam.*, COUNT(question.id) as q_count
        FROM exam
        LEFT JOIN question ON question.exam_id = exam.id
        WHERE exam.user_id = ?
        GROUP BY exam.id
        ORDER BY exam.created_at DESC
        LIMIT 10
    """
    uid = current_user.id

    # Single query: exams + question count via LEFT JOIN
    result = await db.execute(
        select(
            Exam.id,
            Exam.filename,
            Exam.status,
            Exam.created_at,
            func.count(Question.id).label("question_count"),
        )
        .outerjoin(Question, Question.exam_id == Exam.id)
        .where(Exam.user_id == uid)
        .group_by(Exam.id)
        .order_by(Exam.created_at.desc())
        .limit(10)
    )
    rows = result.fetchall()

    activities = [
        {
            "id": row.id,
            "type": "parse",
            "filename": row.filename,
            "status": row.status,
            "question_count": row.question_count,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]

    return {"activities": activities}