"""
IELTS Writing AI grades — read endpoint + manual retry.

Routes:
    GET  /quiz-attempts/{attempt_id}/writing-grades  -> list grades
    POST /quiz-attempts/{attempt_id}/writing-grades/retry/{question_id}
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.db.session import get_db
from app.db.models.quiz_attempt import QuizAttempt
from app.db.models.user import User
from app.db.models.writing_grade import IeltsWritingGrade
from app.services import ielts_writing_grader

logger = logging.getLogger(__name__)
router = APIRouter()


class WritingGradeView(BaseModel):
    question_id: int
    task_type: str
    status: str
    band_ta: Optional[float] = None
    band_cc: Optional[float] = None
    band_lr: Optional[float] = None
    band_gra: Optional[float] = None
    band_overall: Optional[float] = None
    feedback: Optional[dict] = None
    error_message: Optional[str] = None


class WritingGradesResponse(BaseModel):
    attempt_id: int
    grades: list[WritingGradeView]
    all_done: bool


async def _ensure_attempt_access(
    db: AsyncSession, attempt_id: int, user: Optional[User]
) -> QuizAttempt:
    res = await db.execute(select(QuizAttempt).where(QuizAttempt.id == attempt_id))
    attempt = res.scalars().first()
    if not attempt:
        raise HTTPException(status_code=404, detail="Attempt không tồn tại.")
    if attempt.student_id is not None:
        if user is None or user.id != attempt.student_id:
            raise HTTPException(status_code=403, detail="Bạn không có quyền xem attempt này.")
    return attempt


def _to_view(g: IeltsWritingGrade) -> WritingGradeView:
    return WritingGradeView(
        question_id=g.question_id,
        task_type=g.task_type,
        status=g.status,
        band_ta=float(g.band_ta) if g.band_ta is not None else None,
        band_cc=float(g.band_cc) if g.band_cc is not None else None,
        band_lr=float(g.band_lr) if g.band_lr is not None else None,
        band_gra=float(g.band_gra) if g.band_gra is not None else None,
        band_overall=float(g.band_overall) if g.band_overall is not None else None,
        feedback=g.feedback_json,
        error_message=g.error_message,
    )


@router.get("/{attempt_id}/writing-grades", response_model=WritingGradesResponse)
async def get_writing_grades(
    attempt_id: int,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(deps.get_optional_user),
):
    await _ensure_attempt_access(db, attempt_id, user)
    res = await db.execute(
        select(IeltsWritingGrade).where(IeltsWritingGrade.attempt_id == attempt_id)
    )
    grades = res.scalars().all()
    views = [_to_view(g) for g in grades]
    all_done = bool(views) and all(g.status in ("graded", "failed") for g in views)
    return WritingGradesResponse(attempt_id=attempt_id, grades=views, all_done=all_done)


@router.post("/{attempt_id}/writing-grades/retry/{question_id}", response_model=WritingGradeView)
async def retry_writing_grade(
    attempt_id: int,
    question_id: int,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(deps.get_optional_user),
):
    """Reset failed grade → schedule background re-grade."""
    await _ensure_attempt_access(db, attempt_id, user)

    res = await db.execute(
        select(IeltsWritingGrade).where(
            IeltsWritingGrade.attempt_id == attempt_id,
            IeltsWritingGrade.question_id == question_id,
        )
    )
    grade = res.scalars().first()
    if grade and grade.status == "graded":
        return _to_view(grade)
    if grade:
        grade.status = "pending"
        grade.error_message = None
        await db.commit()

    asyncio.create_task(ielts_writing_grader.grade_writing_for_attempt(attempt_id))

    return _to_view(grade) if grade else WritingGradeView(
        question_id=question_id, task_type="task_2", status="pending",
    )
