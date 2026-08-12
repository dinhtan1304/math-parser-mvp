import time
import hashlib
import logging
import threading
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.session import get_db
from app.core import security
from app.core.config import settings
from app.core.redis_client import get_redis
from app.db.models.user import User
from app.schemas.user import TokenPayload

logger = logging.getLogger(__name__)

reusable_oauth2 = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/auth/login"
)

# ── Token blacklist ──
# Backed by Redis when available (survives restart + multi-worker), else an
# in-memory dict with lazy TTL cleanup. Keys are SHA-256 hashes of the token.
_token_blacklist: dict[str, float] = {}
_blacklist_lock = threading.Lock()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def blacklist_token(token: str, ttl_seconds: int | None = None) -> None:
    """Add a token to the blacklist. Tokens auto-expire after TTL."""
    h = _token_hash(token)
    ttl = ttl_seconds or settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60

    r = get_redis()
    if r is not None:
        try:
            await r.setex(f"bl:{h}", ttl, "1")
            return
        except Exception as e:
            logger.warning("Redis blacklist set failed, using in-memory: %s", e)

    # In-memory fallback
    expiry = time.time() + ttl
    with _blacklist_lock:
        _token_blacklist[h] = expiry
        # Cleanup expired entries periodically (every 100 additions)
        if len(_token_blacklist) % 100 == 0:
            now = time.time()
            expired = [k for k, v in _token_blacklist.items() if v < now]
            for k in expired:
                del _token_blacklist[k]


async def is_token_blacklisted(token: str) -> bool:
    """Check if a token has been blacklisted."""
    h = _token_hash(token)

    r = get_redis()
    if r is not None:
        try:
            return bool(await r.exists(f"bl:{h}"))
        except Exception as e:
            logger.warning("Redis blacklist check failed, using in-memory: %s", e)

    # In-memory fallback
    with _blacklist_lock:
        expiry = _token_blacklist.get(h)
        if expiry is None:
            return False
        if expiry < time.time():
            del _token_blacklist[h]
            return False
        return True


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    token: str = Depends(reusable_oauth2)
) -> User:
    # Check token blacklist (logout)
    if await is_token_blacklisted(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
        )

    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
        token_data = TokenPayload(**payload)
    except (JWTError, ValidationError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        )

    # BUG FIX: Guard against None sub before int() conversion
    if not token_data.sub:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        )

    # Refresh token KHÔNG dùng được như access token (chỉ hợp lệ ở /auth/refresh).
    # Token legacy không có type → chấp nhận (coi như access) để không đá phiên cũ.
    if token_data.type == "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token cannot be used for API access",
        )

    # Async query — tài khoản đang chờ xóa (deleted_at) coi như không tồn tại,
    # token đã phát hành trước đó cũng mất hiệu lực ngay.
    result = await db.execute(
        select(User).filter(User.id == int(token_data.sub), User.deleted_at.is_(None))
    )
    user = result.scalars().first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return user

def get_current_active_superuser(
    current_user: User = Depends(get_current_user),
) -> User:
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="The user doesn't have enough privileges"
        )
    return current_user


# Alias — same as get_current_user (active check is already inside)
get_current_active_user = get_current_user


# ── Optional auth (returns None if no token / invalid token) ──
_optional_oauth2 = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/auth/login", auto_error=False
)


async def get_optional_user(
    db: AsyncSession = Depends(get_db),
    token: str | None = Depends(_optional_oauth2),
) -> User | None:
    """Return the current user if a valid token is present, else None."""
    if not token:
        return None
    if await is_token_blacklisted(token):
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[security.ALGORITHM])
        token_data = TokenPayload(**payload)
    except (JWTError, ValidationError):
        return None
    if not token_data.sub or token_data.type == "refresh":
        return None
    result = await db.execute(
        select(User).filter(User.id == int(token_data.sub), User.deleted_at.is_(None))
    )
    user = result.scalars().first()
    if not user or not user.is_active:
        return None
    return user