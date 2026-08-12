"""Quyền của chủ thể dữ liệu (Luật BVDLCN 91/2025, NĐ 356/2025).

  GET  /me/data-export    — xuất toàn bộ dữ liệu cá nhân dạng JSON
  POST /me/delete-request — yêu cầu xóa tài khoản (soft-delete + grace period)
  POST /me/delete-cancel  — hủy yêu cầu xóa bằng mã hủy
  GET  /me/privacy        — trạng thái đồng ý tiếp thị
  PATCH /me/privacy       — bật/tắt đồng ý tiếp thị

Nguyên tắc: quyền xóa dữ liệu là quyền pháp định, KHÔNG được phụ thuộc vào một
cấu hình có thể quên bật. Khi EMAIL_ENABLED=False, luồng xóa vẫn hoàn tất được
bằng xác nhận mật khẩu + gõ lại chuỗi "XOA".
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core import security
from app.core.config import settings
from app.db.models.consent import ConsentLog, GRANTED, WITHDRAWN, MARKETING
from app.db.models.exam import Exam
from app.db.models.lesson_plan import LessonPlan
from app.db.models.question import Question
from app.db.models.user import User
from app.db.session import get_db
from app.services import consent_service

logger = logging.getLogger(__name__)
router = APIRouter()

# Chuỗi người dùng phải gõ lại khi xác nhận xóa mà không dùng email.
DELETE_CONFIRM_PHRASE = "XOA"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── Xuất dữ liệu ─────────────────────────────────────────────────────────────

@router.get("/data-export")
async def export_my_data(
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Trả toàn bộ dữ liệu cá nhân dạng JSON tải về (quyền mang dữ liệu đi)."""
    questions = (await db.execute(
        select(Question).where(Question.user_id == current_user.id)
    )).scalars().all()
    exams = (await db.execute(
        select(Exam).where(Exam.user_id == current_user.id)
    )).scalars().all()
    plans = (await db.execute(
        select(LessonPlan).where(LessonPlan.user_id == current_user.id)
    )).scalars().all()
    consents = (await db.execute(
        select(ConsentLog)
        .where(ConsentLog.user_id == current_user.id)
        .order_by(ConsentLog.created_at.asc())
    )).scalars().all()

    def _dt(v: Optional[datetime]) -> Optional[str]:
        return v.isoformat() if v else None

    bundle: dict[str, Any] = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "profile": {
            "id": current_user.id,
            "email": current_user.email,
            "full_name": current_user.full_name,
            "role": current_user.role,
        },
        "questions": [
            {
                "id": q.id, "question_text": q.question_text,
                "subject_code": q.subject_code, "question_type": q.question_type,
                "topic": q.topic, "difficulty": q.difficulty, "grade": q.grade,
                "chapter": q.chapter, "lesson_title": q.lesson_title,
                "answer": q.answer, "solution_steps": q.solution_steps,
                "is_public": q.is_public, "origin": q.origin, "ai_model": q.ai_model,
                "created_at": _dt(q.created_at),
            }
            for q in questions
        ],
        "exams": [
            {
                "id": e.id, "filename": e.filename, "subject_code": e.subject_code,
                "grade": e.grade, "status": e.status, "origin": e.origin,
                "created_at": _dt(e.created_at),
            }
            for e in exams
        ],
        "lesson_plans": [
            {
                "id": p.id, "title": p.title, "subject_code": p.subject_code,
                "grade": p.grade, "status": p.status, "origin": p.origin,
                "content": json.loads(p.content_json or "{}"),
                "created_at": _dt(p.created_at),
            }
            for p in plans
        ],
        "consents": [
            {
                "consent_type": c.consent_type, "policy_version": c.policy_version,
                "action": c.action, "created_at": _dt(c.created_at),
            }
            for c in consents
        ],
        # Chưa có tính năng thanh toán; giữ khóa để cấu trúc ổn định về sau.
        "payments": [],
    }

    payload = json.dumps(bundle, ensure_ascii=False, indent=2)
    filename = f"edu-smart-app-du-lieu-{current_user.id}.json"
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Xóa tài khoản ────────────────────────────────────────────────────────────

class DeleteRequestBody(BaseModel):
    password: str = Field(..., description="Mật khẩu hiện tại, để xác thực chủ tài khoản")
    confirm_phrase: Optional[str] = Field(
        default=None,
        description=f'Bắt buộc gõ "{DELETE_CONFIRM_PHRASE}" khi hệ thống chưa bật gửi email',
    )


class DeleteRequestOut(BaseModel):
    detail: str
    deleted_at: datetime
    purge_after_days: int
    # Mã hủy chỉ trả về 1 lần. Tài khoản chờ xóa không đăng nhập được nên đây là
    # đường duy nhất để tự khôi phục.
    cancel_token: Optional[str] = None


@router.post("/delete-request", response_model=DeleteRequestOut)
async def request_account_deletion(
    body: DeleteRequestBody,
    request: Request,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Yêu cầu xóa tài khoản: soft-delete + thời gian chờ, có thể hủy."""
    if not security.verify_password(body.password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Mật khẩu không chính xác")

    # Bước xác nhận thứ hai. Có email → gửi mã qua email; chưa bật email → yêu
    # cầu gõ lại chuỗi xác nhận. Không có nhánh nào khiến luồng bế tắc.
    if not settings.EMAIL_ENABLED:
        phrase = (body.confirm_phrase or "").strip().upper()
        if phrase != DELETE_CONFIRM_PHRASE:
            raise HTTPException(
                status_code=400,
                detail=f'Vui lòng gõ "{DELETE_CONFIRM_PHRASE}" để xác nhận xóa tài khoản',
            )

    cancel_token = secrets.token_urlsafe(32)
    current_user.deleted_at = datetime.now(timezone.utc)
    current_user.delete_cancel_token = _hash_token(cancel_token)
    await db.commit()

    from app.core.audit import audit_log
    audit_log(
        "account_delete_requested",
        user_id=current_user.id,
        ip=consent_service.client_ip(request),
    )

    if settings.EMAIL_ENABLED:
        try:
            from app.services.email import send_delete_confirmation
            await send_delete_confirmation(current_user.email, cancel_token)
        except Exception as e:  # pragma: no cover - lỗi email không chặn quyền xóa
            logger.warning("Gửi email xác nhận xóa thất bại: %s", e)

    return DeleteRequestOut(
        detail=(
            "Đã ghi nhận yêu cầu xóa tài khoản. Bạn có "
            f"{settings.ACCOUNT_DELETE_GRACE_DAYS} ngày để hủy bằng mã hủy bên dưới."
        ),
        deleted_at=current_user.deleted_at,
        purge_after_days=settings.ACCOUNT_DELETE_GRACE_DAYS,
        cancel_token=cancel_token,
    )


class DeleteCancelBody(BaseModel):
    email: str
    cancel_token: str


@router.post("/delete-cancel")
async def cancel_account_deletion(
    body: DeleteCancelBody,
    db: AsyncSession = Depends(get_db),
):
    """Hủy yêu cầu xóa. Không yêu cầu đăng nhập vì tài khoản chờ xóa không đăng
    nhập được — xác thực bằng email + mã hủy."""
    user = (await db.execute(
        select(User).where(User.email == body.email)
    )).scalars().first()

    token_hash = _hash_token(body.cancel_token)
    if (
        user is None
        or user.deleted_at is None
        or not user.delete_cancel_token
        or not secrets.compare_digest(user.delete_cancel_token, token_hash)
    ):
        raise HTTPException(status_code=400, detail="Mã hủy không hợp lệ hoặc đã hết hiệu lực")

    user.deleted_at = None
    user.delete_cancel_token = None
    await db.commit()

    from app.core.audit import audit_log
    audit_log("account_delete_cancelled", user_id=user.id)
    return {"detail": "Đã hủy yêu cầu xóa. Tài khoản của bạn hoạt động bình thường trở lại."}


# ── Quyền riêng tư: đồng ý tiếp thị ──────────────────────────────────────────

class PrivacyOut(BaseModel):
    marketing: bool


class PrivacyUpdate(BaseModel):
    marketing: bool


@router.get("/privacy", response_model=PrivacyOut)
async def get_privacy(
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    latest = await consent_service.latest_consents(db, current_user.id)
    entry = latest.get(MARKETING)
    return PrivacyOut(marketing=bool(entry and entry.action == GRANTED))


@router.patch("/privacy", response_model=PrivacyOut)
async def update_privacy(
    body: PrivacyUpdate,
    request: Request,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bật/tắt đồng ý nhận thông tin tiếp thị — ghi thêm dòng nhật ký, không sửa cũ."""
    await consent_service.record_consent(
        db,
        user_id=current_user.id,
        consent_type=MARKETING,
        action=GRANTED if body.marketing else WITHDRAWN,
        request=request,
        commit=True,
    )
    return PrivacyOut(marketing=body.marketing)
