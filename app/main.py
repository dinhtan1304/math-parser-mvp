import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from contextlib import asynccontextmanager
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

logger = logging.getLogger(__name__)

from app.core.config import settings
from app.api import auth, parser, questions, generator, dashboard, export, classes, assignments, submissions, analytics, curriculum, subjects, quizzes, quiz_attempts, media, pages, ielts_parser, ielts_audio, ielts_generator, ielts_writing, lesson_plans
from app.db.session import engine
from app.db.base import Base

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Optional Redis for shared runtime state (degrades to in-memory if absent)
    try:
        from app.core.redis_client import init_redis
        await init_redis()
    except Exception as e:
        logger.warning(f"Redis init skipped: {e}")

    # Startup: create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # ── Safe column migrations (works for both SQLite and PostgreSQL) ──
    # ⚠️ LEGACY MIGRATIONS — từ 2026-07-10 schema mới quản lý bằng Alembic
    # (alembic/versions/, baseline 404cab2e5e0c). Danh sách ALTER dưới đây giữ
    # nguyên cho DB cũ chưa `alembic stamp head`; KHÔNG thêm mục mới vào đây —
    # thêm cột/index mới = sửa model + `alembic revision --autogenerate`.
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
        ("question",    "is_public",   "ALTER TABLE question ADD COLUMN is_public BOOLEAN DEFAULT TRUE"),
        ("user",        "role",        "ALTER TABLE user ADD COLUMN role VARCHAR(20) DEFAULT 'teacher'"),
        ("user",        "reset_token",   "ALTER TABLE \"user\" ADD COLUMN reset_token VARCHAR(128)"),
        ("user",        "reset_token_expires", "ALTER TABLE \"user\" ADD COLUMN reset_token_expires TIMESTAMPTZ"),
        ("question",    "is_bank_duplicate", "ALTER TABLE question ADD COLUMN is_bank_duplicate BOOLEAN DEFAULT FALSE"),
        # ── Multi-subject support ──
        ("curriculum",  "subject_code",  "ALTER TABLE curriculum ADD COLUMN subject_code VARCHAR(30) DEFAULT 'toan'"),
        ("curriculum",  "section_code",  "ALTER TABLE curriculum ADD COLUMN section_code VARCHAR(30) DEFAULT ''"),
        ("question",    "subject_code",  "ALTER TABLE question ADD COLUMN subject_code VARCHAR(30) DEFAULT 'toan'"),
        ("exam",        "subject_code",  "ALTER TABLE exam ADD COLUMN subject_code VARCHAR(30) DEFAULT 'toan'"),
        ("exam",        "grade",         "ALTER TABLE exam ADD COLUMN grade INTEGER"),
        ("exam",        "layout_json",    "ALTER TABLE exam ADD COLUMN layout_json TEXT"),
        ("class",       "subject_code",  "ALTER TABLE class ADD COLUMN subject_code VARCHAR(30)"),
        ("question",    "answer_source", "ALTER TABLE question ADD COLUMN answer_source VARCHAR(20)"),
        # ── Quiz system ──
        ("assignment",  "quiz_id",       "ALTER TABLE assignment ADD COLUMN quiz_id INTEGER REFERENCES quiz(id) ON DELETE SET NULL"),
        # Make student_id nullable for anonymous quiz attempts
        ("quizattempt", "student_id_nullable", "ALTER TABLE quizattempt ALTER COLUMN student_id DROP NOT NULL"),
        # Manual grading support
        ("quizattempt", "graded_by_id", "ALTER TABLE quizattempt ADD COLUMN graded_by_id INTEGER REFERENCES \"user\"(id)"),
        ("quizattempt", "graded_at",    "ALTER TABLE quizattempt ADD COLUMN graded_at TIMESTAMPTZ"),
        ("quizanswer",  "teacher_comment", "ALTER TABLE quizanswer ADD COLUMN teacher_comment VARCHAR(1000)"),
        # Teacher page feature
        ("teacherpage", "view_count",      "ALTER TABLE teacherpage ADD COLUMN view_count INTEGER DEFAULT 0"),
        # IELTS bank support
        ("question",    "extra_data",      "ALTER TABLE question ADD COLUMN extra_data TEXT"),
        ("questiondraft", "page_num",      "ALTER TABLE questiondraft ADD COLUMN page_num INTEGER"),
        # OCR layout — page_num + bbox cho mỗi câu (từ block-aware pipeline)
        ("question",    "page_num",        "ALTER TABLE question ADD COLUMN page_num INTEGER"),
        ("question",    "bbox_json",       "ALTER TABLE question ADD COLUMN bbox_json TEXT"),
    ]
    # OPT: Index migrations (CREATE INDEX IF NOT EXISTS is idempotent)
    _index_migrations = [
        "CREATE INDEX IF NOT EXISTS ix_question_user_created ON question(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_exam_user_created ON exam(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_exam_hash_status ON exam(file_hash, status)",
        # Multi-subject indexes
        "CREATE INDEX IF NOT EXISTS ix_curriculum_subject_grade ON curriculum(subject_code, grade)",
        "CREATE INDEX IF NOT EXISTS ix_question_user_subject ON question(user_id, subject_code)",
        "CREATE INDEX IF NOT EXISTS ix_question_user_subject_grade ON question(user_id, subject_code, grade)",
        # Quiz system indexes
        "CREATE INDEX IF NOT EXISTS ix_assignment_quiz ON assignment(quiz_id)",
        # Community bank: is_public + created_at (query không lọc theo user_id)
        "CREATE INDEX IF NOT EXISTS ix_question_public_created ON question(is_public, created_at DESC)",
    ]
    # Run each migration in its own transaction so a failed ALTER
    # (column already exists) doesn't abort subsequent migrations.
    for table, col, sql in _migrations:
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
            import logging
            logging.getLogger(__name__).info(f"Migration: added {table}.{col}")
        except Exception:
            pass  # Column already exists
    async with engine.begin() as conn:
        for idx_sql in _index_migrations:
            try:
                await conn.execute(text(idx_sql))
            except Exception:
                pass  # Index already exists
        # Teacher-only pivot: normalize legacy/empty roles to 'teacher'
        try:
            await conn.execute(text("UPDATE \"user\" SET role='teacher' WHERE role='user' OR role='student' OR role IS NULL"))
        except Exception:
            pass

    # ── Seed subject + curriculum data ──
    try:
        from app.db.session import AsyncSessionLocal
        from app.db.models.subject import Subject, SUBJECTS_GDPT_2018
        from app.db.models.curriculum import Curriculum, GDPT_2018_MATH

        async with AsyncSessionLocal() as session:
            # Seed/backfill subjects idempotently so new subjects added in code
            # (for example IELTS) appear in existing databases.
            existing_subjects = {
                row[0]
                for row in (await session.execute(text("SELECT subject_code FROM subject"))).fetchall()
            }
            missing_subjects = [
                s for s in SUBJECTS_GDPT_2018
                if s["subject_code"] not in existing_subjects
            ]
            if missing_subjects:
                for s in missing_subjects:
                    session.add(Subject(**s))
                await session.commit()
                logger.info(f"Seeded {len(missing_subjects)} missing subjects")

            # Seed curriculum (if empty)
            cur_count = (await session.execute(text("SELECT COUNT(*) FROM curriculum"))).scalar()
            if cur_count == 0:
                for row in GDPT_2018_MATH:
                    session.add(Curriculum(**row))
                await session.commit()
                logger.info(f"Seeded {len(GDPT_2018_MATH)} curriculum entries")
            else:
                # Backfill subject_code for existing curriculum rows
                await session.execute(
                    text("UPDATE curriculum SET subject_code = 'toan' WHERE subject_code IS NULL")
                )
                await session.commit()

            # Seed YCCĐ (yêu cầu cần đạt) Toán 6 — neo grounding cho KHBD
            try:
                from app.db.seed_yccd import seed_yccd_toan6
                added_yccd = await seed_yccd_toan6(session)
                if added_yccd:
                    logger.info(f"Seeded {added_yccd} YCCĐ entries (Toán 6)")
            except Exception as e:
                logger.warning(f"YCCĐ seed skipped: {e}")

            # Seed phiên bản chính sách — mốc so sánh cho luồng đồng ý lại.
            try:
                from app.services.consent_service import seed_policy_versions
                await seed_policy_versions(session)
            except Exception as e:
                logger.warning(f"Policy version seed skipped: {e}")
    except Exception as e:
        logger.warning(f"Subject/curriculum seed skipped: {e}")

    # ── Constraint migration (PostgreSQL only) ──
    try:
        async with engine.begin() as conn:
            # Drop old curriculum unique constraint, add new one with subject_code
            await conn.execute(text("ALTER TABLE curriculum DROP CONSTRAINT IF EXISTS uq_curriculum"))
            await conn.execute(text(
                "ALTER TABLE curriculum ADD CONSTRAINT uq_curriculum_subject "
                "UNIQUE (subject_code, grade, section_code, chapter_no, lesson_no)"
            ))
            logger.info("Migrated curriculum unique constraint → uq_curriculum_subject")
    except Exception:
        pass  # SQLite: constraints managed by create_all; or already migrated

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
            from app.services.similarity_detector import ensure_similarity_table
            await ensure_similarity_table(engine)
        except Exception as e:
            logger.warning(f"Similarity table init skipped: {e}")
        # Document RAG chunk table (hybrid OCR + RAG pipeline)
        try:
            from app.services.document_rag import ensure_document_chunk_table
            await ensure_document_chunk_table(engine)
        except Exception as e:
            logger.warning(f"Document chunk table init skipped: {e}")
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Vector table init skipped: {e}")

    # D2: best-effort retention cleanup of old uploaded files (0 = disabled)
    try:
        if settings.UPLOAD_RETENTION_DAYS > 0:
            from app.api.parser import cleanup_old_uploads
            cleanup_old_uploads(settings.UPLOAD_RETENTION_DAYS)
    except Exception as e:
        logger.warning(f"Upload retention cleanup skipped: {e}")

    # ── Orphan exam recovery ──
    # Parse chạy bằng BackgroundTasks in-process → crash/restart giữa chừng để
    # exam kẹt pending/processing vĩnh viễn (FE poll mãi). Tại boot, không task
    # nào sống sót qua restart nên mọi exam ở 2 trạng thái đó là mồ côi → đánh
    # failed để user re-upload (OCR artifact cache theo file_hash vẫn còn, chạy
    # lại rất nhanh). ocr_review/needs_review là trạng thái chờ user, KHÔNG đụng.
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(
                "UPDATE exam SET status='failed', "
                "error_message='Server khởi động lại giữa chừng khi đang xử lý. "
                "Vui lòng tải lại file (kết quả OCR đã cache, chạy lại sẽ nhanh).' "
                "WHERE status IN ('pending', 'processing')"
            ))
            recovered = getattr(result, "rowcount", 0) or 0
            if recovered:
                logger.warning(f"Orphan exam recovery: marked {recovered} stuck exam(s) as failed")
    except Exception as e:
        logger.warning(f"Orphan exam recovery skipped: {e}")

    yield
    # Shutdown
    try:
        from app.core.progress_bus import drain_background_tasks
        await drain_background_tasks()
    except Exception as e:
        logger.warning(f"Background task drain skipped: {e}")
    try:
        from app.core.redis_client import close_redis
        await close_redis()
    except Exception:
        pass
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
    expose_headers=["X-Skipped-Duplicates", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-Request-Id"],
)

# Security headers middleware
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # HSTS — instruct browsers to always use HTTPS
        if settings.ENV == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # CSP — restrict resource loading
        csp_parts = [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net",
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
            "img-src 'self' data: blob: https:",
            "media-src 'self' https:",
            ("connect-src 'self' " + " ".join(settings.BACKEND_CORS_ORIGINS)) if settings.BACKEND_CORS_ORIGINS else "connect-src 'self'",
            "font-src 'self' data:",
            "frame-ancestors 'none'",
        ]
        response.headers["Content-Security-Policy"] = "; ".join(csp_parts)
        # Permissions-Policy — disable unused browser features
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response

app.add_middleware(SecurityHeadersMiddleware)

# Request ID middleware — generates unique ID per request
from app.middleware.request_id import RequestIDMiddleware
app.add_middleware(RequestIDMiddleware)

# Rate limiting (Sprint 2, Task 13)
from app.core.rate_limit import RateLimitMiddleware
app.add_middleware(RateLimitMiddleware, enabled=(settings.ENV == "production"))

# Include Routers
app.include_router(auth.router,        prefix=f"{settings.API_V1_STR}/auth",        tags=["auth"])
app.include_router(parser.router,      prefix=f"{settings.API_V1_STR}/parser",      tags=["parser"])
app.include_router(ielts_parser.router, prefix=f"{settings.API_V1_STR}/parser",     tags=["ielts"])
app.include_router(ielts_audio.router,  prefix=f"{settings.API_V1_STR}/parser",     tags=["ielts"])
app.include_router(questions.router,   prefix=f"{settings.API_V1_STR}/questions",   tags=["questions"])
app.include_router(generator.router,   prefix=f"{settings.API_V1_STR}/generate",    tags=["generator"])
app.include_router(ielts_generator.router, prefix=f"{settings.API_V1_STR}/generate", tags=["ielts"])
app.include_router(dashboard.router,   prefix=f"{settings.API_V1_STR}/dashboard",   tags=["dashboard"])
app.include_router(export.router,      prefix=f"{settings.API_V1_STR}/export",      tags=["export"])
app.include_router(classes.router,     prefix=f"{settings.API_V1_STR}/classes",     tags=["classroom"])
# 2026-07-10: đã đổi tên file khớp nội dung — assignments.py = Assignment CRUD,
# submissions.py = Submission records (dormant, teacher-only pivot).
app.include_router(assignments.router, prefix=f"{settings.API_V1_STR}/assignments", tags=["classroom"])
app.include_router(submissions.router, prefix=f"{settings.API_V1_STR}/submissions", tags=["classroom"])
app.include_router(analytics.router,   prefix=f"{settings.API_V1_STR}/analytics",   tags=["analytics"])
app.include_router(curriculum.router,  prefix=f"{settings.API_V1_STR}/curriculum",  tags=["curriculum"])
app.include_router(subjects.router,   prefix=f"{settings.API_V1_STR}/subjects",   tags=["subjects"])
app.include_router(quizzes.router,       prefix=f"{settings.API_V1_STR}/quizzes",      tags=["quiz"])
app.include_router(quiz_attempts.router, prefix=f"{settings.API_V1_STR}/quiz-attempts", tags=["quiz"])
app.include_router(ielts_writing.router, prefix=f"{settings.API_V1_STR}/quiz-attempts", tags=["ielts"])
app.include_router(media.router,         prefix=f"{settings.API_V1_STR}/media",         tags=["media"])
app.include_router(pages.router,         prefix=f"{settings.API_V1_STR}/pages",         tags=["pages"])
app.include_router(lesson_plans.router,  prefix=f"{settings.API_V1_STR}/lesson-plans",  tags=["lesson-plans"])

# Tuân thủ pháp lý: nhật ký đồng ý + quyền chủ thể dữ liệu (Luật BVDLCN 91/2025)
from app.api import consents, me as me_api
app.include_router(consents.router,      prefix=f"{settings.API_V1_STR}/consents",      tags=["compliance"])
app.include_router(me_api.router,        prefix=f"{settings.API_V1_STR}/me",            tags=["compliance"])

# Admin APIs
from app.api import admin
app.include_router(admin.router,         prefix=f"{settings.API_V1_STR}/admin",         tags=["admin"])

# K12 OCR admin endpoint (replaces the old OCR benchmark FE flow)
from app.api import k12_ocr
app.include_router(k12_ocr.router,       prefix=f"{settings.API_V1_STR}/admin",         tags=["admin"])

# Serve uploaded media files (images, audio)
import os
from fastapi.staticfiles import StaticFiles
MEDIA_DIR = "media_uploads"
os.makedirs(MEDIA_DIR, exist_ok=True)
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")

# ── Health check (Sprint 1, Task 8) ──
@app.get("/health", tags=["system"])
async def health_check():
    """Health check for Docker, Railway, and load balancers."""
    import time

    checks = {"status": "ok", "timestamp": time.time()}

    # DB connectivity — do NOT expose error details to public endpoint
    try:
        from app.db.session import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("SELECT 1"))
            result.scalar()
        checks["database"] = "connected"
    except Exception as e:
        logger.warning(f"Health check DB error: {e}")
        checks["database"] = "disconnected"
        checks["status"] = "degraded"

    # Gemini API key configured
    checks["ai_configured"] = bool(settings.GOOGLE_API_KEY)

    return checks
