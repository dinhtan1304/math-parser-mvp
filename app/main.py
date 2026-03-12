import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from contextlib import asynccontextmanager
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

logger = logging.getLogger(__name__)

from app.core.config import settings
from app.api import auth, parser, questions, generator, dashboard, export, classes, assignments, submissions, game, analytics, curriculum, chat, notifications, live
from app.db.session import engine
from app.db.base import Base

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # ── Safe column migrations (works for both SQLite and PostgreSQL) ──
    _migrations = [
        ("exam",     "file_hash",    "ALTER TABLE exam ADD COLUMN file_hash VARCHAR(32)"),
        ("question", "content_hash", "ALTER TABLE question ADD COLUMN content_hash VARCHAR(32)"),
        ("question", "grade",        "ALTER TABLE question ADD COLUMN grade INTEGER"),
        ("question", "chapter",      "ALTER TABLE question ADD COLUMN chapter VARCHAR(200)"),
        ("question", "lesson_title", "ALTER TABLE question ADD COLUMN lesson_title VARCHAR(200)"),
        # Classroom feature columns
        ("class",       "subject",     "ALTER TABLE class ADD COLUMN subject VARCHAR(100)"),
        ("class",       "grade",       "ALTER TABLE class ADD COLUMN grade INTEGER"),
        ("class",       "description", "ALTER TABLE class ADD COLUMN description TEXT"),
        ("assignment",  "description", "ALTER TABLE assignment ADD COLUMN description TEXT"),
        ("submission",  "game_mode",   "ALTER TABLE submission ADD COLUMN game_mode VARCHAR(50)"),
        ("submission",  "xp_earned",   "ALTER TABLE submission ADD COLUMN xp_earned INTEGER DEFAULT 0"),
        ("studentxp",   "level",       "ALTER TABLE studentxp ADD COLUMN level INTEGER DEFAULT 1"),
        ("question",    "is_public",   "ALTER TABLE question ADD COLUMN is_public BOOLEAN DEFAULT TRUE"),
        ("user",        "role",        "ALTER TABLE user ADD COLUMN role VARCHAR(20) DEFAULT 'student'"),
        ("devicetoken", "platform",      "ALTER TABLE devicetoken ADD COLUMN platform VARCHAR(10)"),
        ("user",        "reset_token",   "ALTER TABLE \"user\" ADD COLUMN reset_token VARCHAR(128)"),
        ("user",        "reset_token_expires", "ALTER TABLE \"user\" ADD COLUMN reset_token_expires TIMESTAMPTZ"),
    ]
    # OPT: Index migrations (CREATE INDEX IF NOT EXISTS is idempotent)
    _index_migrations = [
        "CREATE INDEX IF NOT EXISTS ix_question_user_created ON question(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_exam_user_created ON exam(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_exam_hash_status ON exam(file_hash, status)",
    ]
    async with engine.begin() as conn:
        for table, col, sql in _migrations:
            try:
                await conn.execute(text(sql))
                import logging
                logging.getLogger(__name__).info(f"Migration: added {table}.{col}")
            except Exception:
                pass  # Column already exists
        for idx_sql in _index_migrations:
            try:
                await conn.execute(text(idx_sql))
            except Exception:
                pass  # Index already exists
        # Migrate old role="user" → "student"
        try:
            await conn.execute(text("UPDATE \"user\" SET role='student' WHERE role='user' OR role IS NULL"))
        except Exception:
            pass

    # Migrate old broken FTS5 table (had wrong content= definition) — drop and recreate
    try:
        async with engine.begin() as _conn:
            # Check if old FTS table exists with broken content= schema
            _result = await _conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='question_fts'"))
            _row = _result.fetchone()
            if _row and 'content=' in (_row[0] or ''):
                # Old external-content FTS5 table — drop it so init_fts can recreate correctly
                await _conn.execute(text("DROP TABLE IF EXISTS question_fts"))
                import logging
                logging.getLogger(__name__).info("Dropped old FTS5 table with broken content= schema")
    except Exception:
        pass

    # Init FTS5 full-text search index (SQLite only — skipped on PostgreSQL)
    try:
        from app.services.fts import init_fts
        await init_fts(engine)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"FTS5 init skipped: {e}")

    # Init vector embedding table
    try:
        from app.services.vector_search import init_vector_table
        await init_vector_table(engine)
        try:
            from app.services.chat_rag import ensure_chat_tables
            await ensure_chat_tables(engine)
        except Exception as e:
            logger.warning(f"Chat tables init skipped: {e}")
        try:
            from app.services.similarity_detector import ensure_similarity_table
            await ensure_similarity_table(engine)
        except Exception as e:
            logger.warning(f"Similarity table init skipped: {e}")
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Vector table init skipped: {e}")

    yield
    # Shutdown
    await engine.dispose()

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

# FIX #12: CORS — no wildcard fallback in production
# In production, BACKEND_CORS_ORIGINS must be explicitly set.
import logging as _log
_cors_logger = _log.getLogger(__name__)

if settings.ENV == "development":
    cors_origins = ["*"]
elif settings.BACKEND_CORS_ORIGINS:
    cors_origins = settings.BACKEND_CORS_ORIGINS
else:
    # Production with no CORS origins configured — log a warning, restrict to empty list
    # (This blocks all cross-origin requests, which is safer than allowing everything)
    _cors_logger.warning(
        "PRODUCTION: BACKEND_CORS_ORIGINS not configured. "
        "All cross-origin requests will be blocked. "
        "Set BACKEND_CORS_ORIGINS env var to your frontend URL(s)."
    )
    cors_origins = []

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security headers middleware
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response

app.add_middleware(SecurityHeadersMiddleware)

# Rate limiting (Sprint 2, Task 13)
from app.core.rate_limit import RateLimitMiddleware
app.add_middleware(RateLimitMiddleware, enabled=(settings.ENV == "production"))

# Include Routers
app.include_router(auth.router,        prefix=f"{settings.API_V1_STR}/auth",        tags=["auth"])
app.include_router(parser.router,      prefix=f"{settings.API_V1_STR}/parser",      tags=["parser"])
app.include_router(questions.router,   prefix=f"{settings.API_V1_STR}/questions",   tags=["questions"])
app.include_router(generator.router,   prefix=f"{settings.API_V1_STR}/generate",    tags=["generator"])
app.include_router(dashboard.router,   prefix=f"{settings.API_V1_STR}/dashboard",   tags=["dashboard"])
app.include_router(export.router,      prefix=f"{settings.API_V1_STR}/export",      tags=["export"])
app.include_router(classes.router,     prefix=f"{settings.API_V1_STR}/classes",     tags=["classroom"])
# NOTE: file naming is swapped — submissions.py contains assignment CRUD code,
#       assignments.py contains submission/XP/leaderboard code.
#       Routers are mounted with correct semantic prefixes here.
app.include_router(submissions.router, prefix=f"{settings.API_V1_STR}/assignments", tags=["classroom"])
app.include_router(assignments.router, prefix=f"{settings.API_V1_STR}/submissions", tags=["classroom"])
app.include_router(game.router,        prefix=f"{settings.API_V1_STR}/game",        tags=["game"])
app.include_router(analytics.router,   prefix=f"{settings.API_V1_STR}/analytics",   tags=["analytics"])
app.include_router(curriculum.router,  prefix=f"{settings.API_V1_STR}/curriculum",  tags=["curriculum"])
app.include_router(chat.router,          prefix=f"{settings.API_V1_STR}/chat",          tags=["chat"])
app.include_router(notifications.router, prefix=f"{settings.API_V1_STR}/notifications", tags=["notifications"])
app.include_router(live.router,          prefix=f"{settings.API_V1_STR}/live",          tags=["live"])

# Admin APIs
from app.api import admin
app.include_router(admin.router,         prefix=f"{settings.API_V1_STR}/admin",         tags=["admin"])

# ── Health check (Sprint 1, Task 8) ──
@app.get("/health", tags=["system"])
async def health_check():
    """Health check for Docker, Railway, and load balancers."""
    import time

    checks = {"status": "ok", "timestamp": time.time()}

    # DB connectivity
    try:
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("SELECT 1"))
            result.scalar()
        checks["database"] = "connected"
    except Exception as e:
        checks["database"] = f"error: {str(e)[:100]}"
        checks["status"] = "degraded"

    # Gemini API key configured
    checks["ai_configured"] = bool(settings.GOOGLE_API_KEY)

    return checks