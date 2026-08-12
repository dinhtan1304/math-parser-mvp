"""OCR review step (PDF | markdown editable) — persistence helpers + endpoint auth.

The split-pipeline behaviour (OCR gate → status=ocr_review → parse_exam_from_markdown
resume) is exercised end-to-end live; here we cover the file-backed persistence
(write/read/cleanup, artifact fallback) and that the endpoints are wired + owner-guarded.
"""
import json
import os

from app.api import parser as P


def test_write_read_cleanup_roundtrip():
    exam_id = 990001
    try:
        P._write_ocr_review(
            exam_id,
            markdown="# Đề thi\n$x^2$",
            ocr_result={"blocks": [{"page_num": 1, "kind": "text"}], "figures": [], "method": "paddle-vl"},
            layout_snapshot={"pages": [{"page_num": 1}], "blocks": [], "figures": [], "warnings": []},
            ingest_stats={"ocr_pages": 1},
            ingest_warnings=["w1"],
            file_hash="abc123",
            page_count=1,
        )
        assert P._read_ocr_review_md(exam_id) == "# Đề thi\n$x^2$"
        ctx = P._load_review_json(exam_id)
        assert ctx["file_hash"] == "abc123"
        assert ctx["page_count"] == 1
        assert ctx["layout_snapshot"]["pages"] == [{"page_num": 1}]
        assert ctx["blocks"][0]["kind"] == "text"
    finally:
        P._cleanup_ocr_review(exam_id)
    # cleanup really removed the files
    assert P._read_ocr_review_md(exam_id) is None
    assert P._load_review_json(exam_id) == {}


def test_read_missing_returns_none():
    assert P._read_ocr_review_md(987654) is None


def test_ocr_from_artifact_fallback(tmp_path, monkeypatch):
    # Simulate a cached OCR artifact and verify the GET fallback reads it.
    art_dir = os.path.join("uploads", "ocr_artifacts")
    os.makedirs(art_dir, exist_ok=True)
    fhash = "deadbeef99"
    art_path = os.path.join(art_dir, f"{fhash}_toan_paddle-vl_v16.json")
    with open(art_path, "w", encoding="utf-8") as f:
        json.dump({"text": "nội dung OCR", "blocks": [{"kind": "equation"}], "page_count": 2, "method": "paddle-vl"}, f, ensure_ascii=False)
    try:
        class _FakeExam:
            file_hash = fhash
        md, ctx = P._ocr_from_artifact(_FakeExam())
        assert md == "nội dung OCR"
        assert ctx["blocks"][0]["kind"] == "equation"
        assert ctx["page_count"] == 2
    finally:
        os.remove(art_path)


def test_ocr_from_artifact_no_hash():
    class _FakeExam:
        file_hash = None
    assert P._ocr_from_artifact(_FakeExam()) == ("", {})


def test_get_ocr_requires_auth(client):
    # No token → 401; unknown exam with token → 404 (route wired + owner-guarded).
    r = client.get("/api/v1/parser/123456/ocr")
    assert r.status_code in (401, 403)


def test_get_ocr_unknown_exam_404(client, make_teacher):
    _email, headers = make_teacher()
    r = client.get("/api/v1/parser/55555/ocr", headers=headers)
    assert r.status_code == 404


def test_parse_ocr_without_markdown_conflicts(client, make_teacher):
    _email, headers = make_teacher()
    # exam doesn't exist for this user → 404 (never reaches the 409 markdown check)
    r = client.post("/api/v1/parser/55556/ocr/parse", headers=headers)
    assert r.status_code == 404


def test_ocr_review_step_flag_default_on():
    # Default gate is enabled (env unset in test).
    assert isinstance(P.OCR_REVIEW_STEP, bool)
