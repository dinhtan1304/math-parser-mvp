"""
/api/v1/assignments — Teacher sends exams to classes; students view their assignments.
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.api.deps import get_current_active_user
from app.db.session import get_db
from app.db.models.user import User
from app.db.models.classroom import Assignment, Class, ClassMember, Submission
from app.db.models.exam import Exam
from app.schemas.classroom import (
    AssignmentCreate, AssignmentUpdate, AssignmentResponse, SendToClassesRequest,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ─── Teacher: CRUD ───────────────────────────────────────────

@router.post("", response_model=AssignmentResponse, status_code=status.HTTP_201_CREATED)
async def create_assignment(
    payload: AssignmentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    # Verify teacher owns the class
    cls = await _teacher_class_or_404(payload.class_id, current_user.id, db)

    assignment = Assignment(
        class_id=payload.class_id,
        exam_id=payload.exam_id,
        created_by=current_user.id,
        title=payload.title,
        description=payload.description,
        deadline=payload.deadline,
        max_attempts=payload.max_attempts,
        show_answer=payload.show_answer,
    )
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)
    return await _enrich_assignment(assignment, cls.name, db)


@router.post("/send-to-classes", response_model=List[AssignmentResponse])
async def send_to_multiple_classes(
    payload: SendToClassesRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Convenience endpoint: send one exam to multiple classes at once."""
    # Verify exam belongs to teacher
    exam = await db.scalar(
        select(Exam).where(Exam.id == payload.exam_id, Exam.user_id == current_user.id)
    )
    if not exam:
        raise HTTPException(status_code=404, detail="Đề thi không tồn tại")

    created = []
    for class_id in payload.class_ids:
        cls = await _teacher_class_or_404(class_id, current_user.id, db)
        assignment = Assignment(
            class_id=class_id,
            exam_id=payload.exam_id,
            created_by=current_user.id,
            title=payload.title,
            description=payload.description,
            deadline=payload.deadline,
            max_attempts=payload.max_attempts,
            show_answer=payload.show_answer,
        )
        db.add(assignment)
        await db.flush()
        created.append(await _enrich_assignment(assignment, cls.name, db))

    await db.commit()
    return created


@router.get("", response_model=List[AssignmentResponse])
async def list_assignments(
    class_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List assignments created by this teacher, optionally filtered by class."""
    q = select(Assignment, Class).join(Class, Assignment.class_id == Class.id).where(
        Assignment.created_by == current_user.id
    )
    if class_id:
        q = q.where(Assignment.class_id == class_id)
    q = q.order_by(Assignment.created_at.desc())

    result = await db.execute(q)
    out = []
    for assignment, cls in result.all():
        out.append(await _enrich_assignment(assignment, cls.name, db))
    return out


@router.get("/for-student", response_model=List[AssignmentResponse])
async def assignments_for_student(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """All active assignments in classes the current student belongs to."""
    result = await db.execute(
        select(Assignment, Class)
        .join(Class, Assignment.class_id == Class.id)
        .join(ClassMember, ClassMember.class_id == Class.id)
        .where(
            ClassMember.student_id == current_user.id,
            ClassMember.is_active == True,
            Assignment.is_active == True,
        )
        .order_by(Assignment.created_at.desc())
    )
    out = []
    for assignment, cls in result.all():
        out.append(await _enrich_assignment(assignment, cls.name, db))
    return out


@router.get("/{assignment_id}", response_model=AssignmentResponse)
async def get_assignment(
    assignment_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    assignment, cls = await _get_assignment_accessible(assignment_id, current_user.id, db)
    return await _enrich_assignment(assignment, cls.name, db)


@router.patch("/{assignment_id}", response_model=AssignmentResponse)
async def update_assignment(
    assignment_id: int,
    payload: AssignmentUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    assignment = await db.scalar(
        select(Assignment).where(
            Assignment.id == assignment_id,
            Assignment.created_by == current_user.id,
        )
    )
    if not assignment:
        raise HTTPException(status_code=404, detail="Bài tập không tồn tại")
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(assignment, field, val)
    await db.commit()
    await db.refresh(assignment)
    cls = await db.get(Class, assignment.class_id)
    return await _enrich_assignment(assignment, cls.name if cls else "", db)


@router.delete("/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_assignment(
    assignment_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    assignment = await db.scalar(
        select(Assignment).where(
            Assignment.id == assignment_id,
            Assignment.created_by == current_user.id,
        )
    )
    if not assignment:
        raise HTTPException(status_code=404, detail="Bài tập không tồn tại")
    await db.delete(assignment)
    await db.commit()


# ─── Helpers ─────────────────────────────────────────────────

async def _teacher_class_or_404(class_id: int, teacher_id: int, db: AsyncSession) -> Class:
    cls = await db.scalar(
        select(Class).where(Class.id == class_id, Class.teacher_id == teacher_id)
    )
    if not cls:
        raise HTTPException(status_code=404, detail="Lớp học không tồn tại")
    return cls


async def _get_assignment_accessible(
    assignment_id: int, user_id: int, db: AsyncSession
) -> tuple[Assignment, Class]:
    """Return assignment if user is teacher OR enrolled student."""
    result = await db.execute(
        select(Assignment, Class)
        .join(Class, Assignment.class_id == Class.id)
        .where(Assignment.id == assignment_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Bài tập không tồn tại")
    assignment, cls = row

    # Teacher owns it?
    if cls.teacher_id == user_id:
        return assignment, cls

    # Student enrolled?
    enrolled = await db.scalar(
        select(ClassMember).where(
            ClassMember.class_id == cls.id,
            ClassMember.student_id == user_id,
            ClassMember.is_active == True,
        )
    )
    if enrolled:
        return assignment, cls

    raise HTTPException(status_code=403, detail="Bạn không có quyền xem bài tập này")


async def _enrich_assignment(
    assignment: Assignment, class_name: str, db: AsyncSession
) -> AssignmentResponse:
    sub_count = await db.scalar(
        select(func.count()).where(Submission.assignment_id == assignment.id)
    )
    completed_count = await db.scalar(
        select(func.count()).where(
            Submission.assignment_id == assignment.id,
            Submission.status == "completed",
        )
    )
    return AssignmentResponse(
        id=assignment.id,
        class_id=assignment.class_id,
        class_name=class_name,
        exam_id=assignment.exam_id,
        title=assignment.title,
        description=assignment.description,
        deadline=assignment.deadline,
        max_attempts=assignment.max_attempts,
        show_answer=assignment.show_answer,
        is_active=assignment.is_active,
        created_at=assignment.created_at,
        submission_count=sub_count or 0,
        completed_count=completed_count or 0,
    )