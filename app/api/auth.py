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
from app.db.models import consent as consent_models
from app.schemas.user import Token, UserCreate, User as UserSchema
from app.services import consent_service
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

    # Check user — tài khoản đang chờ xóa (deleted_at) coi như không tồn tại.
    result = await db.execute(
        select(User).filter(User.email == form_data.username, User.deleted_at.is_(None))
    )
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
        "refresh_token": security.create_refresh_token(user.id),
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

    # Cột email là UNIQUE nên phải xét cả tài khoản đang chờ xóa: trong thời gian
    # grace, email chưa được giải phóng — chủ cũ vẫn có thể hủy yêu cầu xóa.
    result = await db.execute(select(User).filter(User.email == user_in.email))
    user = result.scalars().first()
    if user is not None and user.deleted_at is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Email này thuộc một tài khoản đang trong thời gian chờ xóa. "
                "Nếu đây là tài khoản của bạn, hãy dùng mã hủy yêu cầu xóa đã được "
                "cấp khi gửi yêu cầu, hoặc liên hệ hỗ trợ."
            ),
        )
    if user:
        raise HTTPException(
            status_code=400,
            detail="Email này đã được đăng ký.",
        )

    # Teacher-only pivot: self-registration always creates a teacher account
    # (enforced by schema Literal["teacher"]).
    user = User(
        email=user_in.email,
        hashed_password=security.get_password_hash(user_in.password),
        full_name=user_in.full_name,
        role=user_in.role,
    )
    db.add(user)
    await db.flush()  # cần user.id để gắn vào nhật ký đồng ý

    # Ghi nhận sự đồng ý ngay trong cùng transaction với việc tạo tài khoản —
    # không tồn tại trạng thái "có tài khoản mà chưa có bằng chứng đồng ý".
    for consent_type in (consent_models.TERMS, consent_models.PRIVACY):
        await consent_service.record_consent(
            db, user_id=user.id, consent_type=consent_type,
            action=consent_models.GRANTED, request=request,
        )
    if user_in.accept_marketing:
        await consent_service.record_consent(
            db, user_id=user.id, consent_type=consent_models.MARKETING,
            action=consent_models.GRANTED, request=request,
        )

    await db.commit()
    await db.refresh(user)
    audit_log("register", user_id=user.id, ip=client_ip, details={
        "role": user_in.role, "marketing": user_in.accept_marketing,
    })
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
    result = await db.execute(
        select(User).filter(User.email == payload.email, User.deleted_at.is_(None))
    )
    user = result.scalars().first()

    # Không tiết lộ email có tồn tại hay không (bảo mật)
    if not user or not user.is_active:
        return {"detail": "Nếu email tồn tại, link đặt lại mật khẩu đã được gửi."}

    token = secrets.token_urlsafe(32)
    # Store hashed token — never store plaintext reset tokens in DB
    user.reset_token = _hash_token(token)
    user.reset_token_expires = datetime.now(timezone.utc) + timedelta(hours=RESET_TOKEN_EXPIRE_HOURS)
    await db.commit()

    # Gửi link đặt lại mật khẩu. Khi email chưa bật, send_reset_email trả về
    # False và chỉ ghi log — response vẫn không tiết lộ email có tồn tại không.
    from app.services.email import send_reset_email
    frontend_url = (settings.BACKEND_CORS_ORIGINS[0] if settings.BACKEND_CORS_ORIGINS else "")
    reset_url = f"{frontend_url}/reset-password?token={token}"
    await send_reset_email(user.email, reset_url)
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
        select(User).filter(User.reset_token == token_hash, User.deleted_at.is_(None))
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


# ── Refresh token endpoint ──────────────────────────────────

class RefreshRequest(BaseModel):
    refresh_token: str


def _refresh_ttl_remaining(payload: dict) -> int:
    """Giây còn lại đến exp của token (để blacklist đúng hạn khi rotate)."""
    exp = payload.get("exp")
    if not exp:
        return settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400
    remaining = int(exp - datetime.now(timezone.utc).timestamp())
    return max(60, remaining)


@router.post("/refresh", response_model=Token)
async def refresh_access_token(
    request: Request,
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Đổi refresh token lấy access token mới. Refresh token bị ROTATE:
    token cũ vào blacklist ngay — dùng lại lần 2 sẽ bị từ chối (chống replay)."""
    from jose import jwt, JWTError
    from app.api.deps import blacklist_token, is_token_blacklisted
    from app.core.audit import audit_log

    client_ip = request.client.host if request.client else "unknown"
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Refresh token không hợp lệ hoặc đã hết hạn",
    )

    try:
        claims = jwt.decode(payload.refresh_token, settings.SECRET_KEY, algorithms=[security.ALGORITHM])
    except JWTError:
        raise invalid
    if claims.get("type") != "refresh" or not claims.get("sub"):
        raise invalid
    if await is_token_blacklisted(payload.refresh_token):
        audit_log("refresh_replay_detected", user_id=int(claims["sub"]), ip=client_ip)
        raise invalid

    result = await db.execute(
        select(User).filter(User.id == int(claims["sub"]), User.deleted_at.is_(None))
    )
    user = result.scalars().first()
    if not user or not user.is_active:
        raise invalid

    # Rotate: thu hồi refresh cũ theo TTL còn lại rồi phát cặp token mới.
    await blacklist_token(payload.refresh_token, ttl_seconds=_refresh_ttl_remaining(claims))
    audit_log("token_refreshed", user_id=user.id, ip=client_ip)
    return {
        "access_token": security.create_access_token(
            user.id, expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        ),
        "refresh_token": security.create_refresh_token(user.id),
        "token_type": "bearer",
    }


# ── Logout endpoint ──────────────────────────────────────────

class LogoutRequest(BaseModel):
    refresh_token: Optional[str] = None


@router.post("/logout", status_code=200)
async def logout(
    request: Request,
    payload: Optional[LogoutRequest] = None,
    current_user: User = Depends(deps.get_current_user),
    token: str = Depends(deps.reusable_oauth2),
) -> Any:
    """Revoke the current access token (và refresh token nếu client gửi kèm)."""
    from app.api.deps import blacklist_token
    from app.core.audit import audit_log

    await blacklist_token(token)
    if payload and payload.refresh_token:
        await blacklist_token(
            payload.refresh_token,
            ttl_seconds=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        )
    client_ip = request.client.host if request.client else "unknown"
    audit_log("logout", user_id=current_user.id, ip=client_ip)
    return {"detail": "Đã đăng xuất thành công"}