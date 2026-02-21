from typing import List, Union, Optional
from pydantic import AnyHttpUrl, field_validator
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
        return env_key

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

    # SECURITY
    SECRET_KEY: str = _get_or_create_secret_key()
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8  # 8 days

    # UPLOAD
    MAX_UPLOAD_SIZE_MB: int = 50  # Max file size in MB

    # CORS
    BACKEND_CORS_ORIGINS: List[AnyHttpUrl] = []

    # EXTERNAL APIS
    GOOGLE_API_KEY: Optional[str] = None
    PORT: int = 8000

    # ENVIRONMENT
    ENV: str = "production"  # "development" or "production"

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