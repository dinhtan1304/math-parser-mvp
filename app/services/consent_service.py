"""Ghi nhận sự đồng ý của người dùng (Luật BVDLCN 91/2025, NĐ 356/2025).

Mọi lần đồng ý / rút lại đồng ý đều để lại một dòng bất biến trong ``consent_log``
kèm phiên bản chính sách, IP và user-agent — đây là bằng chứng tuân thủ khi bị
thanh tra. Không sửa/xóa bản ghi cũ; rút đồng ý = ghi thêm dòng WITHDRAWN.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models.consent import (
    ConsentLog, PolicyVersion, GRANTED, WITHDRAWN,
    CONSENT_TYPES, REQUIRED_CONSENT_TYPES, TERMS, PRIVACY,
)

logger = logging.getLogger(__name__)

# Phiên bản mặc định khi bảng policy_version chưa được seed.
DEFAULT_POLICY_VERSION = "1.0"


def client_ip(request: Optional[Request]) -> Optional[str]:
    """IP thật của client, tôn trọng proxy chỉ khi TRUST_PROXY được bật."""
    if request is None:
        return None
    if getattr(settings, "TRUST_PROXY", False):
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            # Định dạng "client, proxy1, proxy2" → lấy phần tử đầu.
            return fwd.split(",")[0].strip()[:45]
    return (request.client.host if request.client else None)


def client_user_agent(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    ua = request.headers.get("user-agent")
    return ua[:400] if ua else None


async def current_policy_versions(db: AsyncSession) -> dict[str, str]:
    """Phiên bản mới nhất theo từng loại chính sách."""
    rows = (await db.execute(
        select(PolicyVersion).order_by(PolicyVersion.effective_date.asc())
    )).scalars().all()
    # Bản ghi sau ghi đè bản trước → còn lại là phiên bản mới nhất mỗi loại.
    latest = {r.policy_type: r.version for r in rows}
    for t in CONSENT_TYPES:
        latest.setdefault(t, DEFAULT_POLICY_VERSION)
    return latest


async def record_consent(
    db: AsyncSession,
    *,
    user_id: Optional[int],
    consent_type: str,
    action: str = GRANTED,
    policy_version: Optional[str] = None,
    request: Optional[Request] = None,
    commit: bool = False,
) -> ConsentLog:
    """Ghi 1 dòng nhật ký đồng ý. Không commit trừ khi được yêu cầu."""
    if consent_type not in CONSENT_TYPES:
        raise ValueError(f"consent_type không hợp lệ: {consent_type}")
    if action not in (GRANTED, WITHDRAWN):
        raise ValueError(f"action không hợp lệ: {action}")

    if policy_version is None:
        versions = await current_policy_versions(db)
        policy_version = versions.get(consent_type, DEFAULT_POLICY_VERSION)

    row = ConsentLog(
        user_id=user_id,
        consent_type=consent_type,
        policy_version=policy_version,
        action=action,
        ip_address=client_ip(request),
        user_agent=client_user_agent(request),
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    if commit:
        await db.commit()
    return row


async def latest_consents(db: AsyncSession, user_id: int) -> dict[str, ConsentLog]:
    """Bản ghi đồng ý mới nhất của user theo từng loại."""
    rows = (await db.execute(
        select(ConsentLog)
        .where(ConsentLog.user_id == user_id)
        .order_by(ConsentLog.created_at.asc(), ConsentLog.id.asc())
    )).scalars().all()
    latest: dict[str, ConsentLog] = {}
    for r in rows:
        latest[r.consent_type] = r
    return latest


async def stale_consent_types(
    db: AsyncSession,
    user_id: int,
    types: Sequence[str] = REQUIRED_CONSENT_TYPES,
) -> list[str]:
    """Các loại chính sách bắt buộc mà user chưa đồng ý ở phiên bản hiện hành.

    Dùng cho luồng đồng ý lại khi chính sách được cập nhật. Trả về danh sách
    rỗng nghĩa là user đang tuân thủ phiên bản mới nhất.
    """
    versions = await current_policy_versions(db)
    latest = await latest_consents(db, user_id)
    stale: list[str] = []
    for t in types:
        entry = latest.get(t)
        if entry is None or entry.action != GRANTED:
            stale.append(t)
        elif entry.policy_version != versions.get(t, DEFAULT_POLICY_VERSION):
            stale.append(t)
    return stale


async def seed_policy_versions(db: AsyncSession) -> int:
    """Seed phiên bản chính sách ban đầu nếu bảng còn trống.

    Gọi lúc khởi động app, cùng chỗ seed subject/curriculum.
    """
    existing = (await db.execute(select(PolicyVersion.id).limit(1))).first()
    if existing:
        return 0
    now = datetime.now(timezone.utc)
    created = 0
    for policy_type in (TERMS, PRIVACY):
        db.add(PolicyVersion(
            policy_type=policy_type,
            version=DEFAULT_POLICY_VERSION,
            effective_date=now,
            summary="Phiên bản đầu tiên.",
        ))
        created += 1
    await db.commit()
    logger.info("Seeded %d policy versions", created)
    return created
