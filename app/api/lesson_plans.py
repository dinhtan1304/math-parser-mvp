"""
KHBD (Kế hoạch bài dạy CV5512) API.

Endpoints:
    GET    /lesson-plans/lessons          - danh sách bài (dropdown) + cờ has_yccd
    POST   /lesson-plans/generate         - sinh KHBD cho 1 bài, lưu draft
    GET    /lesson-plans                   - danh sách KHBD của giáo viên
    GET    /lesson-plans/{id}             - chi tiết 1 KHBD
    PATCH  /lesson-plans/{id}             - sửa nội dung / trạng thái
    DELETE /lesson-plans/{id}             - xóa
    GET    /lesson-plans/{id}/export      - xuất file Word (.docx)
"""

import io
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core import content_origin
from app.db.session import get_db
from app.db.models.user import User
from app.db.models.curriculum import Curriculum
from app.db.models.lesson_plan import LessonPlan, CurriculumYccd
from app.schemas.lesson_plan import (
    LessonListResponse, LessonOption,
    GenerateKhbdRequest, LessonPlanOut,
    LessonPlanListResponse, LessonPlanSummary,
    UpdateKhbdRequest,
)
from app.services.lesson_plan_generator import generate_khbd

logger = logging.getLogger(__name__)
router = APIRouter()

_VALID_STATUS = {"draft", "generated", "reviewed"}
_VALID_TEMPLATE = {"cv5512", "school"}


def _plan_content(plan: LessonPlan) -> dict:
    try:
        return json.loads(plan.content_json) if plan.content_json else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _to_out(plan: LessonPlan) -> LessonPlanOut:
    content = _plan_content(plan)
    return LessonPlanOut(
        id=plan.id, title=plan.title, subject_code=plan.subject_code,
        grade=plan.grade, bo_sach=plan.bo_sach, so_tiet=plan.so_tiet,
        status=plan.status, template=plan.template, model_used=plan.model_used,
        content=content, warnings=content.get("_warnings", []),
        created_at=plan.created_at, updated_at=plan.updated_at,
        origin=plan.origin or "HUMAN", reviewed_by_user=bool(plan.reviewed_by_user),
    )


@router.get("/lessons", response_model=LessonListResponse)
async def list_lessons(
    grade: int = Query(6, ge=1, le=12),
    subject_code: str = Query("toan"),
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Danh sách bài học (curriculum) cho dropdown, kèm cờ đã ánh xạ YCCĐ."""
    rows = list((await db.execute(
        select(Curriculum).where(
            Curriculum.grade == grade,
            Curriculum.subject_code == subject_code,
            Curriculum.is_active.is_(True),
        ).order_by(Curriculum.chapter_no, Curriculum.lesson_no)
    )).scalars().all())

    # Tập curriculum_id đã có ánh xạ YCCĐ
    mapped = {
        r[0]
        for r in (await db.execute(
            select(CurriculumYccd.curriculum_id).distinct()
        )).fetchall()
    }

    lessons = [
        LessonOption(
            curriculum_id=c.id, chapter_no=c.chapter_no, chapter=c.chapter,
            lesson_no=c.lesson_no, lesson_title=c.lesson_title,
            has_yccd=c.id in mapped,
        )
        for c in rows
    ]
    return LessonListResponse(grade=grade, subject_code=subject_code, lessons=lessons)


@router.post("/generate", response_model=LessonPlanOut)
async def generate_lesson_plan(
    req: GenerateKhbdRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Sinh KHBD cho 1 bài và lưu lại (status=generated)."""
    if req.template not in _VALID_TEMPLATE:
        raise HTTPException(400, f"template không hợp lệ: {req.template}")
    try:
        result = await generate_khbd(
            db, req.curriculum_id, current_user.id,
            so_tiet=req.so_tiet, use_cache=req.use_cache,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        logger.error(f"KHBD generate failed: {e}", exc_info=True)
        raise HTTPException(500, f"Sinh KHBD thất bại: {e}")

    meta = result.get("meta", {})
    plan = LessonPlan(
        user_id=current_user.id,
        curriculum_id=req.curriculum_id,
        title=meta.get("ten_bai", "KHBD"),
        subject_code=result.get("_subject_code", "toan"),
        grade=meta.get("lop"),
        bo_sach=meta.get("bo_sach", "Kết nối tri thức"),
        so_tiet=meta.get("so_tiet", req.so_tiet or 1),
        content_json=json.dumps(result, ensure_ascii=False),
        status="generated",
        template=req.template,
        model_used=result.get("_model"),
        origin=content_origin.AI_GENERATED,
        ai_model=result.get("_model"),
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return _to_out(plan)


@router.get("", response_model=LessonPlanListResponse)
async def list_lesson_plans(
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = list((await db.execute(
        select(LessonPlan).where(LessonPlan.user_id == current_user.id)
        .order_by(LessonPlan.created_at.desc())
    )).scalars().all())
    total = (await db.execute(
        select(func.count()).select_from(LessonPlan)
        .where(LessonPlan.user_id == current_user.id)
    )).scalar() or 0
    items = [
        LessonPlanSummary(
            id=p.id, title=p.title, subject_code=p.subject_code, grade=p.grade,
            so_tiet=p.so_tiet, status=p.status, template=p.template,
            created_at=p.created_at,
            origin=p.origin or "HUMAN", reviewed_by_user=bool(p.reviewed_by_user),
        )
        for p in rows
    ]
    return LessonPlanListResponse(items=items, total=total)


async def _get_owned_plan(plan_id: int, user_id: int, db: AsyncSession) -> LessonPlan:
    plan = (await db.execute(
        select(LessonPlan).where(LessonPlan.id == plan_id)
    )).scalars().first()
    if plan is None or plan.user_id != user_id:
        raise HTTPException(404, "Không tìm thấy KHBD")
    return plan


@router.get("/{plan_id}", response_model=LessonPlanOut)
async def get_lesson_plan(
    plan_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    plan = await _get_owned_plan(plan_id, current_user.id, db)
    return _to_out(plan)


@router.patch("/{plan_id}", response_model=LessonPlanOut)
async def update_lesson_plan(
    plan_id: int,
    req: UpdateKhbdRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    plan = await _get_owned_plan(plan_id, current_user.id, db)
    if req.title is not None:
        plan.title = req.title
    if req.content is not None:
        plan.content_json = json.dumps(req.content, ensure_ascii=False)
    if req.status is not None:
        if req.status not in _VALID_STATUS:
            raise HTTPException(400, f"status không hợp lệ: {req.status}")
        plan.status = req.status
        # Giáo viên duyệt nội dung AI → nhãn chuyển sang "AI có người duyệt".
        if req.status == "reviewed" and content_origin.is_ai_origin(plan.origin):
            plan.origin = content_origin.AI_ASSISTED
            plan.reviewed_by_user = True
            plan.reviewed_at = datetime.now(timezone.utc)
    if req.template is not None:
        if req.template not in _VALID_TEMPLATE:
            raise HTTPException(400, f"template không hợp lệ: {req.template}")
        plan.template = req.template
    await db.commit()
    await db.refresh(plan)
    return _to_out(plan)


@router.delete("/{plan_id}")
async def delete_lesson_plan(
    plan_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    plan = await _get_owned_plan(plan_id, current_user.id, db)
    await db.delete(plan)
    await db.commit()
    return {"ok": True}


@router.get("/{plan_id}/export")
async def export_lesson_plan(
    plan_id: int,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Xuất KHBD ra file Word (.docx) theo template."""
    plan = await _get_owned_plan(plan_id, current_user.id, db)
    content = _plan_content(plan)
    try:
        from app.services.lesson_plan_docx import render_khbd_docx
        docx_bytes = render_khbd_docx(content, template=plan.template)
    except ImportError as e:
        raise HTTPException(501, str(e) or "Thiếu thư viện render Word.")
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"KHBD export failed: {e}", exc_info=True)
        raise HTTPException(500, f"Xuất Word thất bại: {e}")

    safe_title = (plan.title or "KHBD").replace("/", "-").replace("\\", "-")[:80]
    return StreamingResponse(
        io.BytesIO(docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="KHBD_{safe_title}.docx"'},
    )
