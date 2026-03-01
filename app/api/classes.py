"""
/api/v1/classes — Teacher class management + student join.
"""

import random
import string
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.api.deps import get_current_active_user
from app.db.session import get_db
from app.db.models.user import User
from app.db.models.classroom import Class, ClassMember
from app.schemas.classroom import (
    ClassCreate, ClassUpdate, ClassResponse, JoinClassRequest, ClassMemberResponse,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _gen_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


# ─── Helper ──────────────────────────────────────────────────

async def _unique_code(db: AsyncSession) -> str:
    for _ in range(10):
        code = _gen_code()
        exists = await db.scalar(select(func.count()).where(Class.code == code))
        if not exists:
            return code
    raise HTTPException(status_code=500, detail="Không thể tạo mã lớp duy nhất")


# ─── Teacher: CRUD ───────────────────────────────────────────

@router.post("", response_model=ClassResponse, status_code=status.HTTP_201_CREATED)
async def create_class(
    payload: ClassCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    code = await _unique_code(db)
    cls = Class(
        teacher_id=current_user.id,
        name=payload.name,
        subject=payload.subject,
        grade=payload.grade,
        description=payload.description,
        code=code,
    )
    db.add(cls)
    await db.commit()
    await db.refresh(cls)
    return _enrich(cls, 0, 0)


@router.get("", response_model=List[ClassResponse])
async def list_classes(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    result = await db.execute(
        select(Class).where(Class.teacher_id == current_user.id).order_by(Class.created_at.desc())
    )
    classes = result.scalars().all()

    out = []
    for cls in classes:
        m_count = await db.scalar(
            select(func.count()).where(ClassMember.class_id == cls.id, ClassMember.is_active == True)
        )
        a_count = await db.scalar(
            select(func.count())
            .select_from(__import__("app.db.models.classroom", fromlist=["Assignment"]).Assignment)
            .where(__import__("app.db.models.classroom", fromlist=["Assignment"]).Assignment.class_id == cls.id)
        )
        out.append(_enrich(cls, m_count or 0, a_count or 0))
    return out


@router.get("/{class_id}", response_model=ClassResponse)
async def get_class(
    class_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    cls = await _get_class_or_404(class_id, current_user.id, db)
    m_count = await db.scalar(
        select(func.count()).where(ClassMember.class_id == cls.id, ClassMember.is_active == True)
    )
    return _enrich(cls, m_count or 0, 0)


@router.patch("/{class_id}", response_model=ClassResponse)
async def update_class(
    class_id: int,
    payload: ClassUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    cls = await _get_class_or_404(class_id, current_user.id, db)
    for field, val in payload.model_dump(exclude_none=True).items():
        setattr(cls, field, val)
    await db.commit()
    await db.refresh(cls)
    return _enrich(cls, 0, 0)


@router.delete("/{class_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_class(
    class_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    cls = await _get_class_or_404(class_id, current_user.id, db)
    await db.delete(cls)
    await db.commit()


# ─── Members ─────────────────────────────────────────────────

@router.get("/{class_id}/members", response_model=List[ClassMemberResponse])
async def list_members(
    class_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    await _get_class_or_404(class_id, current_user.id, db)
    result = await db.execute(
        select(ClassMember, User)
        .join(User, ClassMember.student_id == User.id)
        .where(ClassMember.class_id == class_id, ClassMember.is_active == True)
        .order_by(ClassMember.joined_at)
    )
    out = []
    for member, user in result.all():
        out.append(ClassMemberResponse(
            id=member.id,
            student_id=member.student_id,
            student_name=user.full_name,
            student_email=user.email,
            joined_at=member.joined_at,
            is_active=member.is_active,
        ))
    return out


@router.delete("/{class_id}/members/{student_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    class_id: int,
    student_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    await _get_class_or_404(class_id, current_user.id, db)
    member = await db.scalar(
        select(ClassMember).where(
            ClassMember.class_id == class_id,
            ClassMember.student_id == student_id,
        )
    )
    if not member:
        raise HTTPException(status_code=404, detail="Học sinh không có trong lớp")
    member.is_active = False
    await db.commit()


# ─── Student: Join by code ────────────────────────────────────

@router.post("/join", status_code=status.HTTP_201_CREATED)
async def join_class(
    payload: JoinClassRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    cls = await db.scalar(
        select(Class).where(Class.code == payload.code.upper(), Class.is_active == True)
    )
    if not cls:
        raise HTTPException(status_code=404, detail="Mã lớp không hợp lệ hoặc lớp đã đóng")

    existing = await db.scalar(
        select(ClassMember).where(
            ClassMember.class_id == cls.id,
            ClassMember.student_id == current_user.id,
        )
    )
    if existing:
        if existing.is_active:
            raise HTTPException(status_code=409, detail="Bạn đã là thành viên của lớp này")
        existing.is_active = True
        await db.commit()
        return {"message": "Đã tham gia lại lớp", "class_id": cls.id, "class_name": cls.name}

    member = ClassMember(class_id=cls.id, student_id=current_user.id)
    db.add(member)
    await db.commit()
    return {"message": "Tham gia lớp thành công", "class_id": cls.id, "class_name": cls.name}


# ─── Student: My classes ─────────────────────────────────────

@router.get("/my/enrolled", response_model=List[ClassResponse])
async def my_enrolled_classes(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    result = await db.execute(
        select(Class)
        .join(ClassMember, ClassMember.class_id == Class.id)
        .where(ClassMember.student_id == current_user.id, ClassMember.is_active == True)
        .order_by(ClassMember.joined_at.desc())
    )
    return [_enrich(c, 0, 0) for c in result.scalars().all()]


# ─── Helpers ─────────────────────────────────────────────────

async def _get_class_or_404(class_id: int, teacher_id: int, db: AsyncSession) -> Class:
    cls = await db.scalar(
        select(Class).where(Class.id == class_id, Class.teacher_id == teacher_id)
    )
    if not cls:
        raise HTTPException(status_code=404, detail="Lớp học không tồn tại")
    return cls


def _enrich(cls: Class, member_count: int, assignment_count: int) -> ClassResponse:
    return ClassResponse(
        id=cls.id,
        name=cls.name,
        subject=cls.subject,
        grade=cls.grade,
        description=cls.description,
        code=cls.code,
        is_active=cls.is_active,
        created_at=cls.created_at,
        member_count=member_count,
        assignment_count=assignment_count,
    )