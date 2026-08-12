"""Gói tuân thủ MVP: đồng ý, quyền chủ thể dữ liệu, soft-delete.

Căn cứ: Luật BVDLCN 91/2025 + NĐ 356/2025.
"""
import json
import secrets

import pytest

API = "/api/v1"


def _register_payload(email: str, **extra):
    payload = {
        "email": email, "password": "Test1234", "full_name": "GV Compliance",
        "accept_terms": True,
    }
    payload.update(extra)
    return payload


def _new_email() -> str:
    return f"c_{secrets.token_hex(4)}@school.vn"


# ── Đồng ý khi đăng ký ────────────────────────────────────────────────────────

def test_register_requires_accepting_terms(client):
    """Không đồng ý điều khoản thì KHÔNG được tạo tài khoản."""
    r = client.post(f"{API}/auth/register", json={
        "email": _new_email(), "password": "Test1234", "full_name": "GV",
        "accept_terms": False,
    })
    assert r.status_code == 422, r.text


def test_register_without_consent_field_is_rejected(client):
    """Thiếu hẳn trường đồng ý cũng bị từ chối — không có mặc định ngầm."""
    r = client.post(f"{API}/auth/register", json={
        "email": _new_email(), "password": "Test1234", "full_name": "GV",
    })
    assert r.status_code == 422, r.text


def test_register_logs_terms_and_privacy_consent(client):
    email = _new_email()
    r = client.post(f"{API}/auth/register", json=_register_payload(email))
    assert r.status_code in (200, 201), r.text

    token = client.post(f"{API}/auth/login", data={
        "username": email, "password": "Test1234",
    }).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    rows = client.get(f"{API}/consents/me", headers=headers).json()
    types = {row["consent_type"] for row in rows}
    assert {"TERMS", "PRIVACY"} <= types
    assert all(row["action"] == "GRANTED" for row in rows)
    # Tiếp thị KHÔNG được bật khi người dùng không chọn.
    assert "MARKETING" not in types


def test_register_records_marketing_only_when_opted_in(client):
    email = _new_email()
    client.post(f"{API}/auth/register", json=_register_payload(email, accept_marketing=True))
    token = client.post(f"{API}/auth/login", data={
        "username": email, "password": "Test1234",
    }).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    rows = client.get(f"{API}/consents/me", headers=headers).json()
    assert "MARKETING" in {row["consent_type"] for row in rows}


# ── Rút lại đồng ý ────────────────────────────────────────────────────────────

def test_marketing_consent_can_be_withdrawn(client, make_teacher):
    _, headers = make_teacher()

    assert client.get(f"{API}/me/privacy", headers=headers).json()["marketing"] is False

    r = client.patch(f"{API}/me/privacy", json={"marketing": True}, headers=headers)
    assert r.status_code == 200 and r.json()["marketing"] is True

    r = client.patch(f"{API}/me/privacy", json={"marketing": False}, headers=headers)
    assert r.status_code == 200 and r.json()["marketing"] is False

    # Rút đồng ý phải để lại dấu vết, không xóa bản ghi cũ.
    rows = client.get(f"{API}/consents/me", headers=headers).json()
    marketing_rows = [r for r in rows if r["consent_type"] == "MARKETING"]
    assert len(marketing_rows) == 2
    assert {r["action"] for r in marketing_rows} == {"GRANTED", "WITHDRAWN"}


def test_consent_log_records_policy_version(client, make_teacher):
    _, headers = make_teacher()
    rows = client.get(f"{API}/consents/me", headers=headers).json()
    assert rows, "phải có ít nhất bản ghi đồng ý lúc đăng ký"
    assert all(row["policy_version"] for row in rows)


def test_consent_status_reports_no_stale_for_new_user(client, make_teacher):
    _, headers = make_teacher()
    body = client.get(f"{API}/consents/status", headers=headers).json()
    assert body["stale"] == []


# ── Quyền mang dữ liệu đi ─────────────────────────────────────────────────────

def test_data_export_returns_full_bundle(client, make_teacher):
    email, headers = make_teacher()

    client.post(f"{API}/questions/bulk", json={
        "questions": [{"question_text": "1 + 1 = ?", "answer": "2"}],
    }, headers=headers)

    r = client.get(f"{API}/me/data-export", headers=headers)
    assert r.status_code == 200, r.text
    assert "attachment" in r.headers.get("content-disposition", "")

    bundle = json.loads(r.content)
    assert bundle["profile"]["email"] == email
    for key in ("questions", "exams", "lesson_plans", "consents", "payments"):
        assert key in bundle
    assert len(bundle["questions"]) == 1
    assert bundle["consents"], "bundle phải kèm lịch sử đồng ý"


# ── Quyền xóa ─────────────────────────────────────────────────────────────────

def test_delete_request_requires_correct_password(client, make_teacher):
    _, headers = make_teacher()
    r = client.post(f"{API}/me/delete-request", json={
        "password": "SaiMatKhau1", "confirm_phrase": "XOA",
    }, headers=headers)
    assert r.status_code == 400


def test_delete_request_requires_confirm_phrase_when_email_disabled(client, make_teacher):
    """EMAIL_ENABLED=False (mặc định test) → phải gõ lại 'XOA', không chết im lặng."""
    _, headers = make_teacher()
    r = client.post(f"{API}/me/delete-request", json={
        "password": "Test1234",
    }, headers=headers)
    assert r.status_code == 400
    assert "XOA" in r.json()["detail"]


def test_delete_request_soft_deletes_and_blocks_all_auth(client, make_teacher):
    email, headers = make_teacher()

    r = client.post(f"{API}/me/delete-request", json={
        "password": "Test1234", "confirm_phrase": "XOA",
    }, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cancel_token"], "phải cấp mã hủy vì tài khoản không đăng nhập lại được"

    # (a) Mọi luồng auth phải loại tài khoản đang chờ xóa.
    assert client.post(f"{API}/auth/login", data={
        "username": email, "password": "Test1234",
    }).status_code == 401
    # Token đã phát hành trước đó cũng mất hiệu lực.
    assert client.get(f"{API}/auth/me", headers=headers).status_code == 404


def test_deleted_account_email_is_blocked_during_grace(client, make_teacher):
    """(b) Email chưa được giải phóng khi tài khoản còn trong thời gian chờ xóa."""
    email, headers = make_teacher()
    client.post(f"{API}/me/delete-request", json={
        "password": "Test1234", "confirm_phrase": "XOA",
    }, headers=headers)

    r = client.post(f"{API}/auth/register", json=_register_payload(email))
    assert r.status_code == 400
    assert "chờ xóa" in r.json()["detail"]


def test_delete_can_be_cancelled_with_token(client, make_teacher):
    email, headers = make_teacher()
    token = client.post(f"{API}/me/delete-request", json={
        "password": "Test1234", "confirm_phrase": "XOA",
    }, headers=headers).json()["cancel_token"]

    r = client.post(f"{API}/me/delete-cancel", json={
        "email": email, "cancel_token": token,
    })
    assert r.status_code == 200, r.text

    # Khôi phục xong thì đăng nhập lại được.
    assert client.post(f"{API}/auth/login", data={
        "username": email, "password": "Test1234",
    }).status_code == 200


def test_delete_cancel_rejects_wrong_token(client, make_teacher):
    email, headers = make_teacher()
    client.post(f"{API}/me/delete-request", json={
        "password": "Test1234", "confirm_phrase": "XOA",
    }, headers=headers)

    r = client.post(f"{API}/me/delete-cancel", json={
        "email": email, "cancel_token": "khong-phai-ma-that",
    })
    assert r.status_code == 400
