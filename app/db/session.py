"""
Database session & engine configuration.

Supports both SQLite (local dev) and PostgreSQL (Neon / production).
"""

import logging
import ssl as _ssl_module
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import event

from app.core.config import settings

logger = logging.getLogger(__name__)

# ── Detect database type ──
_db_url = settings.DATABASE_URL
_is_sqlite = "sqlite" in _db_url
_is_postgres = "postgresql" in _db_url or "postgres" in _db_url

# ── Build engine kwargs ──
_engine_kwargs = {
    "echo": (settings.ENV == "development"),
    "future": True,
}

if _is_sqlite:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

elif _is_postgres:
    # 1) Ensure asyncpg driver
    if "asyncpg" not in _db_url:
        _db_url = _db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        _db_url = _db_url.replace("postgres://", "postgresql+asyncpg://", 1)

    # 2) Strip query params that asyncpg doesn't understand
    #    (sslmode, channel_binding, etc.) — handle SSL via connect_args
    parsed = urlparse(_db_url)
    qs = parse_qs(parsed.query)
    needs_ssl = qs.pop("sslmode", [None])[0] in ("require", "verify-full", "verify-ca")
    qs.pop("ssl", None)
    qs.pop("channel_binding", None)
    clean_query = urlencode({k: v[0] for k, v in qs.items()}, doseq=False)
    _db_url = urlunparse(parsed._replace(query=clean_query))

    # 3) Set SSL via connect_args if needed
    connect_args = {}
    if needs_ssl:
        ssl_ctx = _ssl_module.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = _ssl_module.CERT_NONE
        connect_args["ssl"] = ssl_ctx

    _engine_kwargs["connect_args"] = connect_args
    _engine_kwargs["pool_pre_ping"] = True
    _engine_kwargs["pool_size"] = 5
    _engine_kwargs["max_overflow"] = 10
    _engine_kwargs["pool_recycle"] = 300
    _engine_kwargs["pool_timeout"] = 30

    if "neon" in _db_url.lower():
        _engine_kwargs["pool_size"] = 3
        _engine_kwargs["max_overflow"] = 5
        _engine_kwargs["pool_recycle"] = 180
        logger.info("Neon PostgreSQL detected — optimized pool settings")


engine = create_async_engine(_db_url, **_engine_kwargs)


# ── SQLite PRAGMAs ──
if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA cache_size=-65536")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA mmap_size=134217728")
        cursor.close()
        if not getattr(_set_sqlite_pragmas, '_logged', False):
            logger.info("SQLite PRAGMAs set: WAL, synchronous=NORMAL, cache=64MB, mmap=128MB")
            _set_sqlite_pragmas._logged = True


AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()