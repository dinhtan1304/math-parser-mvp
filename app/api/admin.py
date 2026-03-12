from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, orm
from pydantic import BaseModel
from datetime import datetime

from app.db.session import get_db
from app.db.models.user import User
from app.db.models.question import Question
from app.db.models.exam import Exam
from app.api.deps import get_current_active_superuser
from app.schemas.question import QuestionResponse

router = APIRouter()

# Schemas
class UserPublic(BaseModel):
    id: int
    email: str
    full_name: str | None = None
    role: str
    is_active: bool
    created_at: Any = None

    class Config:
        from_attributes = True

class DashboardStats(BaseModel):
    total_users: int
    total_questions: int
    total_exams: int
    active_users: int

class UserUpdate(BaseModel):
    role: str | None = None
    is_active: bool | None = None

class PaginatedUsers(BaseModel):
    total: int
    items: List[UserPublic]

class PaginatedQuestions(BaseModel):
    total: int
    page: int
    page_size: int
    items: List[dict]

# ─── Statistics ──────────────────────────────────────────────────────────

@router.get("/stats", response_model=DashboardStats)
async def get_admin_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser)
) -> Any:
    # Get total users
    res = await db.execute(select(func.count(User.id)))
    total_users = res.scalar() or 0
    
    # Active users
    res = await db.execute(select(func.count(User.id)).where(User.is_active == True))
    active_users = res.scalar() or 0
    
    # Total questions
    res = await db.execute(select(func.count(Question.id)))
    total_questions = res.scalar() or 0
    
    # Total exams
    res = await db.execute(select(func.count(Exam.id)))
    total_exams = res.scalar() or 0
    
    return {
        "total_users": total_users,
        "total_questions": total_questions,
        "total_exams": total_exams,
        "active_users": active_users
    }

# ─── Users Management ────────────────────────────────────────────────────

@router.get("/users", response_model=PaginatedUsers)
async def list_users(
    skip: int = 0,
    limit: int = 20,
    search: Optional[str] = None,
    role: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser)
) -> Any:
    query = select(User)
    
    if search:
        query = query.where(func.lower(User.email).like(f"%{search.lower()}%") | func.lower(User.full_name).like(f"%{search.lower()}%"))
    if role:
        query = query.where(User.role == role)
        
    total_res = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_res.scalar() or 0
    
    query = query.order_by(desc(User.id)).offset(skip).limit(limit)
    res = await db.execute(query)
    users = res.scalars().all()
    
    # Manually serialize to validate against schema and avoid Pydantic serialization errors
    items = []
    for u in users:
        items.append({
            "id": u.id,
            "email": u.email,
            "full_name": u.full_name,
            "role": u.role,
            "is_active": getattr(u, 'is_active', True) if getattr(u, 'is_active', None) is not None else True,
            "created_at": u.created_at.isoformat() if hasattr(u, 'created_at') and u.created_at else None
        })
    
    return {
        "total": total,
        "items": items
    }

@router.patch("/users/{user_id}", response_model=UserPublic)
async def update_user(
    user_id: int,
    data: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser)
) -> Any:
    res = await db.execute(select(User).filter(User.id == user_id))
    user = res.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    if data.role is not None:
        user.role = data.role
    if data.is_active is not None:
        user.is_active = data.is_active
        
    await db.commit()
    await db.refresh(user)
    return UserPublic.model_validate(user)

# ─── Questions Management ────────────────────────────────────────────────

@router.get("/questions", response_model=PaginatedQuestions)
async def admin_list_questions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser)
) -> Any:
    query = select(Question, User.email.label("author_email")).outerjoin(User, Question.user_id == User.id)
    count_query = select(func.count(Question.id))
    
    if search:
        query = query.where(func.lower(Question.question_text).like(f"%{search.lower()}%"))
        count_query = count_query.where(func.lower(Question.question_text).like(f"%{search.lower()}%"))
        
    total_res = await db.execute(count_query)
    total = total_res.scalar() or 0
    
    query = query.order_by(desc(Question.created_at)).offset((page - 1) * page_size).limit(page_size)
    res = await db.execute(query)
    rows = res.all()
    
    items = []
    for q_obj, email in rows:
        # Since we use List[dict] we explicitly create dictionary payload
        # Pydantic schema validator logic for 'solution_steps' must be simulated or manually coerced
        q_dict = {
            "id": q_obj.id,
            "exam_id": q_obj.exam_id,
            "user_id": q_obj.user_id,
            "question_text": q_obj.question_text,
            "question_type": q_obj.question_type,
            "topic": q_obj.topic,
            "difficulty": q_obj.difficulty,
            "grade": q_obj.grade,
            "chapter": q_obj.chapter,
            "lesson_title": q_obj.lesson_title,
            "answer": q_obj.answer,
            "solution_steps": [],
            "question_order": getattr(q_obj, 'question_order', 0) or 0,
            "is_public": getattr(q_obj, 'is_public', True) if getattr(q_obj, 'is_public', None) is not None else True,
            "author_email": email,
            "created_at": q_obj.created_at.isoformat() if q_obj.created_at else None
        }
        items.append(q_dict)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }

@router.delete("/questions/{question_id}")
async def admin_delete_question(
    question_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser)
) -> Any:
    res = await db.execute(select(Question).filter(Question.id == question_id))
    q = res.scalars().first()
    if not q:
        raise HTTPException(status_code=404, detail="Question not found")
    
    await db.delete(q)
    await db.commit()
    return {"detail": "Question deleted"}

class BulkVisibilityRequest(BaseModel):
    question_ids: List[int]
    is_public: bool

@router.patch("/questions/bulk-visibility")
async def admin_bulk_visibility(
    payload: BulkVisibilityRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser)
) -> Any:
    if not payload.question_ids:
        raise HTTPException(status_code=400, detail="No question IDs provided")
    
    from sqlalchemy import update as sa_update
    stmt = sa_update(Question).where(Question.id.in_(payload.question_ids)).values(is_public=payload.is_public)
    res = await db.execute(stmt)
    await db.commit()
    
    return {"detail": f"Updated {res.rowcount} questions.", "updated": res.rowcount}

from app.schemas.question import QuestionUpdate
@router.put("/questions/{question_id}", response_model=QuestionResponse)
async def admin_update_question(
    question_id: int,
    update: QuestionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser)
) -> Any:
    res = await db.execute(select(Question).filter(Question.id == question_id))
    q = res.scalars().first()
    if not q:
        raise HTTPException(status_code=404, detail="Question not found")
        
    update_data = update.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")
        
    for field, value in update_data.items():
        setattr(q, field, value)
        
    await db.commit()
    await db.refresh(q)
    
    # Return as dict to avoid standard pydantic validation errors
    email_res = await db.execute(select(User.email).where(User.id == q.user_id))
    author_email = email_res.scalar()
    
    q_dict = {
        "id": q.id,
        "exam_id": q.exam_id,
        "user_id": q.user_id,
        "question_text": q.question_text,
        "question_type": q.question_type,
        "topic": q.topic,
        "difficulty": q.difficulty,
        "grade": q.grade,
        "chapter": q.chapter,
        "lesson_title": q.lesson_title,
        "answer": q.answer,
        "solution_steps": [],
        "question_order": getattr(q, 'question_order', 0) or 0,
        "is_public": getattr(q, 'is_public', True) if getattr(q, 'is_public', None) is not None else True,
        "author_email": author_email,
        "created_at": q.created_at.isoformat() if q.created_at else None
    }
    return q_dict
