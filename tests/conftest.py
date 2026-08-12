"""
Shared pytest fixtures.

IMPORTANT: environment is forced to a local SQLite DB + in-memory runtime
state BEFORE the app is imported, so tests never touch the real Postgres/Neon
database or require Redis. Env vars take precedence over the project .env file.
"""
import os

# ── Force a safe test environment (must run before app import) ──
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_pytest.db"
os.environ["ENV"] = "development"          # disables the rate-limit middleware globally
os.environ["REDIS_URL"] = ""               # force in-memory fallback for blacklist/SSE/rate-limit
os.environ.setdefault("GOOGLE_API_KEY", "dummy")

import pathlib
import secrets
import asyncio
import pytest


@pytest.fixture(scope="session")
def event_loop():
    """Single event loop for entire session (avoids 'attached to different loop' errors)."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def client():
    """A TestClient bound to the FastAPI app.

    Used as a context manager so the app lifespan (table creation, subject /
    curriculum seeding) runs against the throwaway SQLite database.
    """
    from starlette.testclient import TestClient

    db_path = pathlib.Path("_pytest.db")
    if db_path.exists():
        db_path.unlink()

    from app.main import app

    with TestClient(app) as c:
        yield c

    # Best-effort cleanup (file may stay briefly locked on Windows)
    try:
        db_path.unlink()
    except OSError:
        pass


@pytest.fixture
def make_teacher(client):
    """Factory: register + login a fresh teacher, return (email, headers)."""
    def _make():
        email = f"t_{secrets.token_hex(4)}@school.vn"
        client.post("/api/v1/auth/register", json={
            "email": email, "password": "Test1234", "full_name": "GV Test",
            # Đăng ký bắt buộc phải đồng ý điều khoản (Luật BVDLCN 91/2025).
            "accept_terms": True,
        })
        r = client.post("/api/v1/auth/login", data={
            "username": email, "password": "Test1234",
        })
        token = r.json()["access_token"]
        return email, {"Authorization": f"Bearer {token}"}
    return _make
