"""Alembic env — tái dùng async engine của app, metadata từ app.db.base.

Cách dùng (chạy từ repo root, cùng env/.env với app):
  - DB mới:        alembic upgrade head
  - DB đang chạy (đã có schema qua create_all + startup-ALTER cũ):
                   alembic stamp head   # đánh dấu baseline, KHÔNG chạy DDL
  - Sau khi sửa model: alembic revision --autogenerate -m "mô tả"
                       → review file sinh ra → alembic upgrade head

Import `app.db.session.engine` để kế thừa nguyên logic URL (asyncpg convert,
strip sslmode/channel_binding, NullPool cho Neon) — không duplicate config.
"""
from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy.engine import Connection

from alembic import context

# App importable khi chạy `alembic` từ repo root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings  # noqa: E402
from app.db.base import Base  # noqa: E402  (import mọi model vào metadata)
from app.db.session import engine as app_engine  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Offline mode (--sql): sinh SQL script, không cần kết nối DB."""
    url = settings.DATABASE_URL
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=url.startswith("sqlite"),
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite không ALTER được nhiều thứ — batch mode tạo bảng tạm + copy
        render_as_batch=connection.dialect.name == "sqlite",
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    async with app_engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await app_engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
