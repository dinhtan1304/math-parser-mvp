from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from contextlib import asynccontextmanager
import os

from app.core.config import settings
from app.api import auth, parser, questions, generator, dashboard, export
from app.db.session import engine
from app.db.base import Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # ── Safe column migrations (works for both SQLite and PostgreSQL) ──
    _migrations = [
        ("exam", "file_hash", "ALTER TABLE exam ADD COLUMN file_hash VARCHAR(32)"),
        ("question", "content_hash", "ALTER TABLE question ADD COLUMN content_hash VARCHAR(32)"),
        ("question", "grade", "ALTER TABLE question ADD COLUMN grade INTEGER"),
        ("question", "chapter", "ALTER TABLE question ADD COLUMN chapter VARCHAR(200)"),
        ("question", "lesson_title", "ALTER TABLE question ADD COLUMN lesson_title VARCHAR(200)"),
    ]
    async with engine.begin() as conn:
        for table, col, sql in _migrations:
            try:
                await conn.execute(text(sql))
                import logging
                logging.getLogger(__name__).info(f"Migration: added {table}.{col}")
            except Exception:
                pass  # Column already exists

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

# CORS
cors_origins = ["*"] if settings.ENV == "development" else (
    [str(origin) for origin in settings.BACKEND_CORS_ORIGINS] 
    if settings.BACKEND_CORS_ORIGINS 
    else ["*"]  # Fallback if no origins configured
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting (Sprint 2, Task 13)
from app.core.rate_limit import RateLimitMiddleware
app.add_middleware(RateLimitMiddleware, enabled=(settings.ENV == "production"))

# Include Routers
app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["auth"])
app.include_router(parser.router, prefix=f"{settings.API_V1_STR}/parser", tags=["parser"])
app.include_router(questions.router, prefix=f"{settings.API_V1_STR}/questions", tags=["questions"])
app.include_router(generator.router, prefix=f"{settings.API_V1_STR}/generate", tags=["generator"])
app.include_router(dashboard.router, prefix=f"{settings.API_V1_STR}/dashboard", tags=["dashboard"])
app.include_router(export.router, prefix=f"{settings.API_V1_STR}/export", tags=["export"])

# Templates
templates = Jinja2Templates(directory="app/templates")

# Static files (Sprint 3, Task 17)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


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

# Frontend Routes
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})