from typing import List, Union, Optional
from pydantic import AnyHttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

import secrets

class Settings(BaseSettings):
    PROJECT_NAME: str = "Math Exam Parser"
    API_V1_STR: str = "/api/v1"
    
    # DATABASE
    # Using SQLite for MVP. For production, switch to PostgreSQL
    DATABASE_URL: str = "sqlite+aiosqlite:///./math_parser.db"
    
    # SECURITY
    SECRET_KEY: str = secrets.token_urlsafe(32)  # Auto-generate if not set via env
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8  # 8 days
    
    # CORS
    BACKEND_CORS_ORIGINS: List[AnyHttpUrl] = []
    
    # EXTERNAL APIS
    GOOGLE_API_KEY: Optional[str] = None
    PORT: int = 8000
    
    # ENVIRONMENT
    ENV: str = "production"  # "development" or "production"

    @field_validator("SECRET_KEY", mode="after")
    def warn_default_secret(cls, v: str) -> str:
        if v == "changethis-secret-key-in-production-please-use-openssl-rand-base64-32":
            import warnings
            warnings.warn(
                "⚠️ Using default SECRET_KEY! Set SECRET_KEY env variable in production.",
                stacklevel=2,
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
        extra="ignore"
    )

settings = Settings()