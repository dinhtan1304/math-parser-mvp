"""
Integration tests for the teacher-only pivot + T1 (exam matrix).

Runs against the FastAPI app via Starlette TestClient on a throwaway SQLite DB
(configured in conftest.py). No external services required.
"""

API = "/api/v1"

# Endpoints that were removed in the teacher-only pivot.
REMOVED_PATH_FRAGMENTS = ["/chat", "/game", "/live", "/notifications"]
REMOVED_ENDPOINTS = [
    "/chat/sessions",
    "/game/modes",
    "/submissions/leaderboard/1",
    "/submissions/xp/me",
]
# Core teacher endpoints that must stay reachable.
KEPT_ENDPOINTS = ["/dashboard", "/subjects", "/classes", "/parser/history"]


# ─── Pivot: routers removed ──────────────────────────────────────────────────

def test_removed_routers_absent_from_openapi(client):
    paths = client.get(f"{API}/openapi.json").json()["paths"]
    leaked = [
        p for p in paths
        if any(frag in p for frag in REMOVED_PATH_FRAGMENTS)
    ]
    assert leaked == [], f"Removed routers still mounted: {leaked}"


def test_kept_routers_present_in_openapi(client):
    """Guard the A0 'keep' decisions: quiz-attempts (needed by IELTS writing
    grading) and the new exam-matrix endpoint must stay mounted."""
    paths = client.get(f"{API}/openapi.json").json()["paths"]
    assert "/api/v1/generate/exam-matrix" in paths
    assert any(p.startswith("/api/v1/quiz-attempts") for p in paths)
    assert any("writing-grades" in p for p in paths)  # IELTS writing grading
    assert any(p.startswith("/api/v1/quizzes") for p in paths)


def test_removed_endpoints_return_404(client, make_teacher):
    _, headers = make_teacher()
    for ep in REMOVED_ENDPOINTS:
        r = client.get(f"{API}{ep}", headers=headers)
        assert r.status_code == 404, f"{ep} should be gone, got {r.status_code}"


def test_kept_endpoints_reachable(client, make_teacher):
    _, headers = make_teacher()
    for ep in KEPT_ENDPOINTS:
        r = client.get(f"{API}{ep}", headers=headers)
        assert r.status_code < 400, f"{ep} should work, got {r.status_code}"


# ─── Pivot: teacher-only auth ────────────────────────────────────────────────

def test_register_creates_teacher_role(client):
    email = "role_check@school.vn"
    r = client.post(f"{API}/auth/register", json={
        "email": email, "password": "Test1234", "full_name": "GV",
        "accept_terms": True,
    })
    assert r.status_code in (200, 201), r.text
    assert r.json()["role"] == "teacher"


def test_register_rejects_student_role(client):
    """role is pinned to teacher; sending student must be rejected (422)."""
    r = client.post(f"{API}/auth/register", json={
        "email": "stud@school.vn", "password": "Test1234",
        "full_name": "S", "role": "student", "accept_terms": True,
    })
    assert r.status_code == 422


# ─── R1a: JWT blacklist on logout (in-memory fallback) ───────────────────────

def test_logout_revokes_token(client, make_teacher):
    _, headers = make_teacher()
    assert client.get(f"{API}/auth/me", headers=headers).status_code == 200
    assert client.post(f"{API}/auth/logout", headers=headers).status_code == 200
    # Token is now blacklisted → 401
    assert client.get(f"{API}/auth/me", headers=headers).status_code == 401


# ─── T1: exam matrix ─────────────────────────────────────────────────────────

GRADE = 6
SUBJECT = "toan"
CH1 = "Chương I. Số tự nhiên"
CH2 = "Chương II. Số nguyên"


def _seed_bank(client, headers):
    """Seed: CH1 → 3 NB, 2 TH, 1 VD ; CH2 → 2 NB."""
    qs = []

    def mk(chapter, diff, n):
        for i in range(n):
            qs.append({
                "question_text": f"{chapter} {diff} #{i+1}: 1+{i}=?",
                "subject_code": SUBJECT, "question_type": "TN",
                "difficulty": diff, "grade": GRADE, "chapter": chapter,
                "answer": "A",
            })

    mk(CH1, "NB", 3); mk(CH1, "TH", 2); mk(CH1, "VD", 1)
    mk(CH2, "NB", 2)
    r = client.post(f"{API}/questions/bulk", json={"questions": qs}, headers=headers)
    assert r.status_code in (200, 201), r.text


def test_exam_matrix_happy_with_deficit(client, make_teacher):
    _, headers = make_teacher()
    _seed_bank(client, headers)

    body = {
        "title": "KT giữa kỳ", "subject_code": SUBJECT, "grade": GRADE,
        "allow_partial": True, "fill_from_adjacent_difficulty": False,
        "cells": [
            {"chapter": CH1, "counts": {"NB": 3, "TH": 2, "VD": 3, "VDC": 0}},
            {"chapter": CH2, "counts": {"NB": 1}},
        ],
    }
    r = client.post(f"{API}/generate/exam-matrix", json=body, headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()

    # Requested 3+2+3+1 = 9 but VD has only 1 → 7 questions, 1 deficit cell.
    assert data["question_count"] == 7
    assert len(data["deficits"]) == 1
    d = data["deficits"][0]
    assert d["chapter"] == CH1 and d["difficulty"] == "VD"
    assert d["requested"] == 3 and d["found"] == 1

    # The new exam actually holds 7 questions.
    q = client.get(
        f"{API}/questions",
        params={"exam_id": data["exam_id"], "my_only": "true", "page_size": 50},
        headers=headers,
    ).json()
    assert q["total"] == 7


def test_exam_matrix_allow_partial_false_returns_400(client, make_teacher):
    _, headers = make_teacher()
    _seed_bank(client, headers)
    body = {
        "title": "Strict", "subject_code": SUBJECT, "grade": GRADE,
        "allow_partial": False,
        "cells": [{"chapter": CH1, "counts": {"VD": 5}}],  # only 1 available
    }
    r = client.post(f"{API}/generate/exam-matrix", json=body, headers=headers)
    assert r.status_code == 400
    assert "deficits" in r.json()["detail"]


def test_exam_matrix_fill_from_adjacent(client, make_teacher):
    _, headers = make_teacher()
    _seed_bank(client, headers)
    # CH2 has NB:2 only. Ask TH:2 with fill → adjacency TH↔NB → 2 from NB, no deficit.
    body = {
        "title": "Fill", "subject_code": SUBJECT, "grade": GRADE,
        "allow_partial": True, "fill_from_adjacent_difficulty": True,
        "cells": [{"chapter": CH2, "counts": {"TH": 2}}],
    }
    r = client.post(f"{API}/generate/exam-matrix", json=body, headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["question_count"] == 2
    assert data["deficits"] == []


def test_exam_matrix_validation(client, make_teacher):
    _, headers = make_teacher()
    cases = [
        {"title": "x", "grade": GRADE, "cells": [{"chapter": "C", "counts": {"NB": "x"}}]},   # non-int
        {"title": "x", "grade": GRADE, "cells": [{"chapter": "C", "counts": {"NB": 999}}]},   # over cap (>300)
        {"title": "x", "grade": GRADE, "cells": [{"chapter": "C", "counts": {"NB": 0}}]},     # total < 1
    ]
    for body in cases:
        r = client.post(f"{API}/generate/exam-matrix", json=body, headers=headers)
        assert r.status_code == 422, f"expected 422 for {body}, got {r.status_code}"
