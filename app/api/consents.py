"""Endpoint ghi nhận và tra cứu sự đồng ý (Luật BVDLCN 91/2025).

  POST /consents         — ghi 1 lần đồng ý / rút đồng ý
  GET  /consents/me      — lịch sử đồng ý của chính người dùng
  GET  /consents/status  — loại chính sách nào đang cần đồng ý lại
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.db.models.consent import (
    ConsentLog, CONSENT_TYPES, GRANTED, WITHDRAWN,
)
from app.db.models.user import User
from app.db.session import get_db
from app.services import consent_service

router = APIRouter()


class ConsentCreate(BaseModel):
    consent_type: str = Field(..., description="TERMS | PRIVACY | MARKETING | CONTENT_LICENSE")
    action: str = Field(default=GRANTED, description="GRANTED | WITHDRAWN")


class ConsentOut(BaseModel):
    id: int
    consent_type: str
    policy_version: str
    action: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ConsentStatusOut(BaseModel):
    """Loại chính sách bắt buộc mà người dùng cần đồng ý (lại)."""
    stale: List[str]
    versions: dict[str, str]


@router.post("", response_model=ConsentOut, status_code=201)
async def create_consent(
    payload: ConsentCreate,
    request: Request,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.consent_type not in CONSENT_TYPES:
        raise HTTPException(400, f"consent_type không hợp lệ: {payload.consent_type}")
    if payload.action not in (GRANTED, WITHDRAWN):
        raise HTTPException(400, f"action không hợp lệ: {payload.action}")

    row = await consent_service.record_consent(
        db,
        user_id=current_user.id,
        consent_type=payload.consent_type,
        action=payload.action,
        request=request,
        commit=True,
    )
    await db.refresh(row)
    return row


@router.get("/me", response_model=List[ConsentOut])
async def my_consents(
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(ConsentLog)
        .where(ConsentLog.user_id == current_user.id)
        .order_by(ConsentLog.created_at.desc(), ConsentLog.id.desc())
    )).scalars().all()
    return rows


@router.get("/status", response_model=ConsentStatusOut)
async def consent_status(
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """FE gọi sau khi đăng nhập để biết có cần hiện modal đồng ý lại không."""
    stale = await consent_service.stale_consent_types(db, current_user.id)
    versions = await consent_service.current_policy_versions(db)
    return ConsentStatusOut(stale=stale, versions=versions)
