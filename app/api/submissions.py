"""
/api/v1/submissions — Xem bài nộp của một bài tập (phía giáo viên).

Còn ĐÚNG MỘT endpoint: GET /assignment/{assignment_id}.

Các endpoint dành cho học sinh (POST nộp bài, GET /my) đã gỡ 2026-08-12 —
chỉ app mathplay-mobile gọi chúng và app đó đã ngừng. Không còn tạo được tài
khoản học sinh (main.py ép mọi role về 'teacher' mỗi lần boot), nên bảng
Submission KHÔNG CÓ NGUỒN DỮ LIỆU MỚI: endpoint còn lại luôn trả rỗng với dữ
liệu hiện tại.

Giữ lại vì đây là đường đọc đúng cho phía giáo viên, và là điểm neo khi làm
sổ điểm TT22/27 (bảng `grade_entry` sẽ cho nhập tay hoặc liên kết ngược).
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from app.api.deps import get_current_active_user
from app.db.session import get_db
from app.db.models.user import User
from app.db.models.classroom import Assignment, Class, Submission
from app.schemas.classroom import SubmissionResponse

router = APIRouter()
logger = logging.getLogger(__name__)


# ─── ĐÃ GỠ: endpoint dành cho học sinh ───────────────────────
# POST /submissions (nộp bài) đã gỡ (2026-08-12). Chỉ app mathplay-mobile gọi,
# app đó đã ngừng; và không còn tạo được tài khoản học sinh để nộp.
#
# Bảng Submission + AnswerDetail GIỮ NGUYÊN (không drop) để không mất dữ liệu
# lịch sử. Sổ điểm TT22/27 sẽ đi đường khác: bảng `grade_entry` cho phép giáo
# viên nhập tay hoặc liên kết ngược về quizattempt.


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


# ĐÃ GỠ (2026-08-12): GET /my — bài nộp của chính học sinh đang đăng nhập.
# Chỉ app mathplay-mobile gọi.


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
