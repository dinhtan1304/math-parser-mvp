"""
Push notification token registration.

POST /notifications/token  — register / update Expo push token for current user
DELETE /notifications/token — remove token (logout / disable notifications)
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete

from app.api import deps
from app.db.session import get_db
from app.db.models.user import User
from app.db.models.notification import DeviceToken

router = APIRouter()


class TokenRegisterRequest(BaseModel):
    expo_push_token: str
    platform: str = "android"  # 'ios' | 'android'


@router.post("/token", status_code=200)
async def register_token(
    payload: TokenRegisterRequest,
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register or update Expo push token for current user.

    Upsert: if token already exists for this user, update platform + timestamp.
    """
    token = payload.expo_push_token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="Token cannot be empty")

    # Check if already registered
    existing = await db.scalar(
        select(DeviceToken).where(
            DeviceToken.user_id == current_user.id,
            DeviceToken.expo_push_token == token,
        )
    )
    if existing:
        existing.platform = payload.platform
        await db.commit()
        return {"detail": "Token updated"}

    # Remove old tokens for this user (keep only latest)
    await db.execute(
        delete(DeviceToken).where(DeviceToken.user_id == current_user.id)
    )

    dt = DeviceToken(
        user_id=current_user.id,
        expo_push_token=token,
        platform=payload.platform,
    )
    db.add(dt)
    await db.commit()
    return {"detail": "Token registered"}


@router.delete("/token", status_code=200)
async def remove_token(
    current_user: User = Depends(deps.get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove all push tokens for current user (e.g. on logout)."""
    await db.execute(
        delete(DeviceToken).where(DeviceToken.user_id == current_user.id)
    )
    await db.commit()
    return {"detail": "Tokens removed"}
