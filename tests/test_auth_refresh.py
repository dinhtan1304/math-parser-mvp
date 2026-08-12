"""
Refresh token flow (P1 2026-07-10):
  - /auth/login trả cặp access + refresh
  - /auth/refresh: đổi refresh → cặp mới; refresh cũ bị rotate (replay → 401)
  - refresh token KHÔNG dùng được như access token
  - /auth/logout kèm refresh_token thu hồi cả hai
"""
import secrets


def _register_login(client):
    email = f"rt_{secrets.token_hex(4)}@school.vn"
    client.post("/api/v1/auth/register", json={
        "email": email, "password": "Test1234", "full_name": "GV Refresh",
        "accept_terms": True,
    })
    r = client.post("/api/v1/auth/login", data={"username": email, "password": "Test1234"})
    assert r.status_code == 200
    return r.json()


def test_login_returns_refresh_token(client):
    tok = _register_login(client)
    assert tok["access_token"]
    assert tok["refresh_token"]
    assert tok["access_token"] != tok["refresh_token"]


def test_refresh_rotates_and_blocks_replay(client):
    tok = _register_login(client)

    r1 = client.post("/api/v1/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert r1.status_code == 200
    new_pair = r1.json()
    assert new_pair["access_token"] and new_pair["refresh_token"]
    assert new_pair["refresh_token"] != tok["refresh_token"]

    # Access token mới dùng được
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new_pair['access_token']}"})
    assert me.status_code == 200

    # Replay refresh token cũ (đã rotate) → 401
    r2 = client.post("/api/v1/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert r2.status_code == 401


def test_refresh_token_rejected_as_access_token(client):
    tok = _register_login(client)
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tok['refresh_token']}"})
    assert r.status_code == 401


def test_garbage_refresh_token_rejected(client):
    r = client.post("/api/v1/auth/refresh", json={"refresh_token": "not-a-jwt"})
    assert r.status_code == 401


def test_logout_revokes_refresh_token(client):
    tok = _register_login(client)
    headers = {"Authorization": f"Bearer {tok['access_token']}"}

    r = client.post("/api/v1/auth/logout", headers=headers, json={"refresh_token": tok["refresh_token"]})
    assert r.status_code == 200

    # Access token đã bị blacklist
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
    # Refresh token cũng bị thu hồi
    assert client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tok["refresh_token"]}
    ).status_code == 401
