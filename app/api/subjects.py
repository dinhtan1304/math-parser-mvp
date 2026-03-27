"""
subjects.py — API endpoint cho danh sách môn học K12 (GDPT 2018).
GET /api/v1/subjects       — Toàn bộ môn học
GET /api/v1/subjects?grade=7 — Lọc theo lớp
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.api.deps import get_db
from app.db.models.subject import Subject

router = APIRouter()


@router.get("")
async def list_subjects(
    grade: Optional[int] = Query(None, ge=1, le=12, description="Lọc môn theo lớp"),
    category: Optional[str] = Query(None, description="bat_buoc | lua_chon | tich_hop"),
    db: AsyncSession = Depends(get_db),
):
    """
    Trả về danh sách môn học. Có thể lọc theo grade và category.
    """
    stmt = (
        select(Subject)
        .where(Subject.is_active == True)
        .order_by(Subject.display_order)
    )

    if grade is not None:
        stmt = stmt.where(Subject.grade_min <= grade, Subject.grade_max >= grade)

    if category is not None:
        stmt = stmt.where(Subject.category == category)

    rows = (await db.execute(stmt)).scalars().all()

    return [
        {
            "subject_code": s.subject_code,
            "name_vi": s.name_vi,
            "name_short": s.name_short,
            "name_en": s.name_en,
            "category": s.category,
            "grade_min": s.grade_min,
            "grade_max": s.grade_max,
            "parent_code": s.parent_code,
            "display_order": s.display_order,
            "icon": s.icon,
        }
        for s in rows
    ]
