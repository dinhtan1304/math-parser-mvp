import hashlib
import secrets
import logging
from datetime import timedelta, datetime, timezone
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.session import get_db
from app.core import security
from app.core.config import settings
from app.db.models.user import User
from app.schemas.user import Token, UserCreate, User as UserSchema
from app.api import deps


def _hash_token(token: str) -> str:
    """Hash a token with SHA256 for secure storage."""
    return hashlib.sha256(token.encode()).hexdigest()

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/login", response_model=Token)
async def login_access_token(
    request: Request,
    db: AsyncSession = Depends(get_db),
    form_data: OAuth2PasswordRequestForm = Depends()
) -> Any:
    from app.core.audit import audit_log
    client_ip = request.client.host if request.client else "unknown"

    # Check user
    result = await db.execute(select(User).filter(User.email == form_data.username))
    user = result.scalars().first()

    if not user or not security.verify_password(form_data.password, user.hashed_password):
        audit_log("login_failed", ip=client_ip, details={"email": form_data.username})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email hoặc mật khẩu không chính xác")

    if not user.is_active:
        audit_log("login_inactive", user_id=user.id, ip=client_ip)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Tài khoản đã bị vô hiệu hóa")

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    audit_log("login_success", user_id=user.id, ip=client_ip)
    return {
        "access_token": security.create_access_token(
            user.id, expires_delta=access_token_expires
        ),
        "token_type": "bearer",
    }

@router.post("/register", response_model=UserSchema)
async def register_user(
    *,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user_in: UserCreate,
) -> Any:
    from app.core.audit import audit_log
    client_ip = request.client.host if request.client else "unknown"

    result = await db.execute(select(User).filter(User.email == user_in.email))
    user = result.scalars().first()
    if user:
        raise HTTPException(
            status_code=400,
            detail="Email này đã được đăng ký.",
        )

    # Role is platform-based: mobile sends "student", web sends "teacher"
    # Only "student" and "teacher" allowed (enforced by schema Literal)
    user = User(
        email=user_in.email,
        hashed_password=security.get_password_hash(user_in.password),
        full_name=user_in.full_name,
        role=user_in.role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    audit_log("register", user_id=user.id, ip=client_ip, details={"role": user_in.role})
    return user

@router.get("/me", response_model=UserSchema)
async def read_users_me(
    current_user: User = Depends(deps.get_current_user),
) -> Any:
    return current_user


# ── Password reset schemas ────────────────────────────────────

class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


# ── Password reset endpoints ──────────────────────────────────

RESET_TOKEN_EXPIRE_HOURS = 1

@router.post("/forgot-password", status_code=200)
async def forgot_password(
    payload: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Gửi link đặt lại mật khẩu qua email (nếu email tồn tại)."""
    result = await db.execute(select(User).filter(User.email == payload.email))
    user = result.scalars().first()

    # Không tiết lộ email có tồn tại hay không (bảo mật)
    if not user or not user.is_active:
        return {"detail": "Nếu email tồn tại, link đặt lại mật khẩu đã được gửi."}

    token = secrets.token_urlsafe(32)
    # Store hashed token — never store plaintext reset tokens in DB
    user.reset_token = _hash_token(token)
    user.reset_token_expires = datetime.now(timezone.utc) + timedelta(hours=RESET_TOKEN_EXPIRE_HOURS)
    await db.commit()

    # TODO: tích hợp email service (SendGrid / Resend / SMTP) để gửi link
    # reset_url = f"{settings.FRONTEND_URL}/reset-password?token={token}"
    # await send_reset_email(user.email, reset_url)
    logger.info(f"Password reset requested for user {user.id}")

    return {"detail": "Nếu email tồn tại, link đặt lại mật khẩu đã được gửi."}


@router.post("/reset-password", status_code=200)
async def reset_password(
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Đặt lại mật khẩu bằng token hợp lệ."""
    # Hash the incoming token to match against stored hash
    token_hash = _hash_token(payload.token)
    result = await db.execute(
        select(User).filter(User.reset_token == token_hash)
    )
    user = result.scalars().first()

    if not user or user.reset_token_expires is None:
        raise HTTPException(status_code=400, detail="Token không hợp lệ hoặc đã hết hạn")

    expires = user.reset_token_expires
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Token không hợp lệ hoặc đã hết hạn")

    # Validate new password (same rules as UserCreate)
    new_pw = payload.new_password
    if len(new_pw) < 8:
        raise HTTPException(status_code=422, detail="Mật khẩu phải có ít nhất 8 ký tự")
    import re
    if not re.search(r"[A-Za-z]", new_pw):
        raise HTTPException(status_code=422, detail="Mật khẩu phải có ít nhất 1 chữ cái")
    if not re.search(r"\d", new_pw):
        raise HTTPException(status_code=422, detail="Mật khẩu phải có ít nhất 1 chữ số")

    user.hashed_password = security.get_password_hash(new_pw)
    user.reset_token = None
    user.reset_token_expires = None
    await db.commit()

    logger.info(f"Password reset completed for user {user.id}")
    return {"detail": "Mật khẩu đã được đặt lại thành công"}


# ── Logout endpoint ──────────────────────────────────────────

@router.post("/logout", status_code=200)
async def logout(
    request: Request,
    current_user: User = Depends(deps.get_current_user),
    token: str = Depends(deps.reusable_oauth2),
) -> Any:
    """Revoke the current access token."""
    from app.api.deps import blacklist_token
    from app.core.audit import audit_log

    blacklist_token(token)
    client_ip = request.client.host if request.client else "unknown"
    audit_log("logout", user_id=current_user.id, ip=client_ip)
    return {"detail": "Đã đăng xuất thành công"}