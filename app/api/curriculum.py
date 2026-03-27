"""
curriculum.py — API endpoint cho chương trình học GDPT 2018 (đa môn).
GET /api/v1/curriculum/tree  — Trả về cây chương/bài theo môn + lớp,
                               kèm số câu hỏi trong ngân hàng (nếu có).
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.api.deps import get_db, get_current_user
from app.db.models.curriculum import Curriculum
from app.db.models.question import Question
from app.db.models.user import User

router = APIRouter()


@router.get("/tree")
async def get_curriculum_tree(
    subject_code: str = Query("toan", description="Ma mon hoc: toan, vat-li, khtn, ..."),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Trả về toàn bộ cây chương trình:
    {
      grades: [
        {
          grade: 6,
          question_count: 42,
          chapters: [
            {
              chapter_no: 1,
              chapter: "Chương I. ...",
              question_count: 10,
              lessons: [
                { id: 1, lesson_title: "...", lesson_no: 1, question_count: 3 }
              ]
            }
          ]
        }
      ]
    }
    """
    # Lấy curriculum rows theo môn, sắp xếp
    cur_rows = (await db.execute(
        select(Curriculum)
        .where(Curriculum.is_active == True, Curriculum.subject_code == subject_code)
        .order_by(Curriculum.grade, Curriculum.chapter_no, Curriculum.lesson_no)
    )).scalars().all()

    # Đếm câu hỏi trong ngân hàng của user hiện tại cho môn này
    q_counts = (await db.execute(
        select(
            Question.grade,
            Question.chapter,
            Question.lesson_title,
            func.count(Question.id).label("cnt"),
        )
        .where(Question.user_id == current_user.id)
        .where(Question.subject_code == subject_code)
        .group_by(Question.grade, Question.chapter, Question.lesson_title)
    )).all()

    # Build lookup: (grade, chapter, lesson_title) → count
    q_map: dict = {}
    for row in q_counts:
        key = (row.grade, (row.chapter or "").strip(), (row.lesson_title or "").strip())
        q_map[key] = row.cnt

    # Assemble tree
    grades_map: dict = {}
    for row in cur_rows:
        g = row.grade
        if g not in grades_map:
            grades_map[g] = {"grade": g, "question_count": 0, "chapters": {}}

        ch_key = row.chapter_no
        if ch_key not in grades_map[g]["chapters"]:
            grades_map[g]["chapters"][ch_key] = {
                "chapter_no": row.chapter_no,
                "chapter": row.chapter,
                "question_count": 0,
                "lessons": [],
            }

        # Câu hỏi khớp với bài này
        lq = q_map.get((g, row.chapter.strip(), row.lesson_title.strip()), 0)
        grades_map[g]["chapters"][ch_key]["lessons"].append({
            "id": row.id,
            "lesson_no": row.lesson_no,
            "lesson_title": row.lesson_title,
            "question_count": lq,
        })
        grades_map[g]["chapters"][ch_key]["question_count"] += lq
        grades_map[g]["question_count"] += lq

    # Convert to list
    result = []
    for g in sorted(grades_map.keys()):
        gd = grades_map[g]
        gd["chapters"] = sorted(gd["chapters"].values(), key=lambda c: c["chapter_no"])
        result.append(gd)

    return {"grades": result}