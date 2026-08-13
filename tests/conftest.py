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


@pytest.fixture
def make_admin(client, make_teacher):
    """Factory: tài khoản quản trị (role='admin'), trả về (email, headers).

    KHÔNG có API nào để tự phong quản trị (đúng như vậy), nên fixture ghi thẳng
    vào DB test rồi đăng nhập lại để token mang role mới.

    Dùng sqlite3 đồng bộ thay vì AsyncSessionLocal: engine async của app gắn
    với event loop riêng, còn test ở đây chạy sync — mở kết nối riêng tránh
    hẳn chuyện "attached to a different loop".
    """
    import sqlite3

    def _make():
        email, _ = make_teacher()

        con = sqlite3.connect("_pytest.db")
        try:
            con.execute('UPDATE "user" SET role = ? WHERE email = ?', ("admin", email))
            con.commit()
        finally:
            con.close()

        # Đăng nhập lại: token cũ đã phát trước khi đổi role.
        r = client.post("/api/v1/auth/login", data={
            "username": email, "password": "Test1234",
        })
        return email, {"Authorization": f"Bearer {r.json()['access_token']}"}

    return _make
