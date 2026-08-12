from typing import List, Union, Optional
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

import os
import secrets
import logging

logger = logging.getLogger(__name__)

# ─── SECRET_KEY persistence ──────────────────────────────────
# If no SECRET_KEY env var, generate once and save to file.
# This ensures tokens survive server restarts.

_SECRET_KEY_FILE = ".secret_key"


def _get_or_create_secret_key() -> str:
    """
    Priority:
      1. SECRET_KEY env var (set by user / Railway / Docker)
      2. .secret_key file (auto-generated, persists across restarts)
      3. Generate new key + save to file
    """
    # 1. Env var — highest priority
    env_key = os.getenv("SECRET_KEY", "").strip()
    if env_key:
        if len(env_key) >= 32:
            return env_key
        # Too short to be safe. Fall through to file/generated path so the
        # app can still start, but warn loudly so the operator notices.
        logger.warning(
            "SECRET_KEY env var is too short (need >=32 chars, got %d). "
            "Ignoring it and falling back to %s.",
            len(env_key), _SECRET_KEY_FILE,
        )

    # 2. Persisted file
    if os.path.exists(_SECRET_KEY_FILE):
        try:
            with open(_SECRET_KEY_FILE, "r") as f:
                file_key = f.read().strip()
            if len(file_key) >= 32:
                return file_key
        except Exception:
            pass

    # 3. Generate + persist
    new_key = secrets.token_urlsafe(48)
    try:
        with open(_SECRET_KEY_FILE, "w") as f:
            f.write(new_key)
        os.chmod(_SECRET_KEY_FILE, 0o600)  # Owner read/write only
        logger.warning(
            "⚠️  Generated new SECRET_KEY (saved to %s). "
            "For production, set SECRET_KEY as an environment variable.",
            _SECRET_KEY_FILE,
        )
    except Exception as e:
        logger.warning("Could not persist SECRET_KEY to file: %s", e)
    return new_key


# ─── Upload limits ───────────────────────────────────────────

MAX_UPLOAD_SIZE_MB: int = 50  # Default 50MB


class Settings(BaseSettings):
    PROJECT_NAME: str = "Math Exam Parser"
    API_V1_STR: str = "/api/v1"

    # DATABASE
    DATABASE_URL: str = "sqlite+aiosqlite:///./math_parser.db"

    # REDIS — optional. When set, shared runtime state (JWT blacklist,
    # rate-limit counters, SSE one-time tokens) is stored in Redis so it
    # survives restarts and works across multiple workers. When unset (or
    # unreachable), the app transparently falls back to in-memory state.
    REDIS_URL: Optional[str] = None

    # SECURITY
    SECRET_KEY: str = _get_or_create_secret_key()
    ALGORITHM: str = "HS256"
    # Access ngắn (1 ngày) + refresh dài (14 ngày, rotate mỗi lần dùng).
    # Trước đây access 8 ngày không refresh — lộ token là mất 8 ngày.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 1 day
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14

    # UPLOAD
    MAX_UPLOAD_SIZE_MB: int = 50  # Max file size in MB

    # C3b: per-user daily AI token quota for the parse pipeline (0 = disabled).
    # Counts estimated input+output tokens across today's exams for the user.
    DAILY_TOKEN_QUOTA: int = 0

    # D3: reject PDFs with more than N pages at upload (0 = disabled). Guards
    # against a huge/decompression-bomb PDF triggering a 30-min OCR + costly
    # Gemini parse before any limit is hit.
    MAX_PDF_PAGES: int = 0

    # D2: delete original uploaded files older than N days on startup (0 =
    # disabled, keep forever). The OCR artifact cache (uploads/ocr_artifacts/)
    # is NOT touched, so re-uploading the same file still skips OCR.
    UPLOAD_RETENTION_DAYS: int = 0

    # ── TUÂN THỦ DỮ LIỆU CÁ NHÂN (Luật BVDLCN 91/2025) ──
    # Số ngày chờ trước khi xóa vĩnh viễn tài khoản đã yêu cầu xóa. Trong thời
    # gian này người dùng còn hủy được bằng mã hủy.
    ACCOUNT_DELETE_GRACE_DAYS: int = 7
    # Bài làm của khách (không tài khoản) được ẩn danh sau N tháng. 0 = tắt.
    GUEST_ANON_MONTHS: int = 12
    # Bật gửi email thật. Khi False, luồng xóa tài khoản KHÔNG chết: chuyển sang
    # xác nhận bằng mật khẩu + gõ lại chuỗi xác nhận (xem app/api/me.py).
    EMAIL_ENABLED: bool = False
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_FROM: Optional[str] = None

    # CORS
    BACKEND_CORS_ORIGINS: List[str] = []

    # EXTERNAL APIS
    GOOGLE_API_KEY: Optional[str] = None
    GEMINI_MODEL: str = "gemini-2.5-flash"  # Default Gemini model for parsing
    PORT: int = 8000

    # ENVIRONMENT
    ENV: str = "production"  # "development" or "production"

    # SECURITY — proxy trust
    # Set TRUST_PROXY=true only when running behind a reverse proxy (nginx, Railway, etc.)
    # that injects a trusted X-Forwarded-For header.
    # When False (default for direct server), X-Forwarded-For is IGNORED to prevent IP spoofing.
    TRUST_PROXY: bool = False

    @property
    def MAX_UPLOAD_BYTES(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    @field_validator("SECRET_KEY", mode="after")
    def validate_secret_key(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError(
                "SECRET_KEY must be at least 32 characters. "
                "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        return v

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> List[str]:
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",")]
        elif isinstance(v, (list, str)):
            return v
        raise ValueError(v)

    model_config = SettingsConfigDict(
        case_sensitive=True,
        env_file=".env",
        extra="ignore",
    )


settings = Settings()