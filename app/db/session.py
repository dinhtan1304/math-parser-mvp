"""
Database session & engine configuration.

SQLite optimizations (Sprint 1, Task 5):
    - WAL mode: allows concurrent reads during writes
    - synchronous=NORMAL: 2x faster writes with minimal risk
    - cache_size=64MB: reduce disk I/O
    - busy_timeout=5s: retry on lock instead of failing immediately
    - mmap_size=128MB: memory-mapped I/O for faster reads
"""

import logging
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import event

from app.core.config import settings

logger = logging.getLogger(__name__)

# ── Build engine args ──
_is_sqlite = "sqlite" in settings.DATABASE_URL

_connect_args = {}
if _is_sqlite:
    _connect_args["check_same_thread"] = False

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=(settings.ENV == "development"),
    future=True,
    connect_args=_connect_args,
    # SQLite doesn't benefit from pool, but PostgreSQL does
    pool_pre_ping=not _is_sqlite,
)


# ── SQLite PRAGMAs — run once per raw connection ──
if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")       # Concurrent reads + writes
        cursor.execute("PRAGMA synchronous=NORMAL")      # Safe + fast
        cursor.execute("PRAGMA cache_size=-65536")       # 64MB page cache
        cursor.execute("PRAGMA busy_timeout=5000")       # 5s retry on lock
        cursor.execute("PRAGMA foreign_keys=ON")         # Enforce FK constraints
        cursor.execute("PRAGMA temp_store=MEMORY")       # Temp tables in RAM
        cursor.execute("PRAGMA mmap_size=134217728")     # 128MB memory-mapped I/O
        cursor.close()
        # Log once on first connection
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