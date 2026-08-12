"""
Tests cho tính năng KHBD (Kế hoạch bài dạy CV5512).

Không cần Gemini thật: kiểm validator, render Word, logic ánh xạ (pure) và
API guard (bài chưa map YCCĐ → 400). Dùng `client` + `make_teacher` từ conftest.
"""

import asyncio

from docx import Document
from docx.oxml.ns import qn

from app.services.lesson_plan_schema import validate_khbd, KHBD_SCHEMA
from app.services.lesson_plan_docx import render_khbd_docx


# ── Fixtures dữ liệu ────────────────────────────────────────────────────────

def _valid_khbd():
    return {
        "meta": {"ten_bai": "Phép nhân và phép chia phân số", "mon": "Toán",
                 "lop": 6, "bo_sach": "Kết nối tri thức", "so_tiet": 2,
                 "chuong": "Chương VI. Phân số"},
        "muc_tieu": {
            "kien_thuc": ["Thực hiện được phép nhân, chia hai phân số "
                          "$\\frac{a}{b} \\times \\frac{c}{d} = \\frac{ac}{bd}$"],
            "nang_luc": {
                "chung": [{"ten": "Giải quyết vấn đề và sáng tạo", "bieu_hien": "x"}],
                "dac_thu": [{"ten": "Tư duy và lập luận toán học", "bieu_hien": "y"}],
            },
            "pham_chat": [{"ten": "Chăm chỉ", "bieu_hien": "z"}],
            "yccd_refs": ["TOAN6.PHANSO.04"],
        },
        "thiet_bi_day_hoc": ["Máy chiếu"],
        "tien_trinh": [
            {"loai": "mo_dau", "ten_hoat_dong": "Khởi động", "thoi_gian_phut": 8,
             "muc_tieu": "a", "noi_dung": {"mo_ta": "m", "cau_hoi_nhiem_vu": ["Tính $\\frac{2}{3} \\times \\frac{6}{5}$"]},
             "san_pham": {"mo_ta": "s", "ket_qua_mong_doi": ["$= \\frac{4}{5}$"]},
             "to_chuc_thuc_hien": {"chuyen_giao_nhiem_vu": "1", "thuc_hien_nhiem_vu": "2",
                                   "bao_cao_thao_luan": "3", "ket_luan_nhan_dinh": "4"}},
            {"loai": "hinh_thanh_kien_thuc", "ten_hoat_dong": "KT", "thoi_gian_phut": 20,
             "muc_tieu": "a", "noi_dung": {"mo_ta": "m", "cau_hoi_nhiem_vu": ["$\\frac{a}{b} \\times \\frac{c}{d}$"]},
             "san_pham": {"mo_ta": "s", "ket_qua_mong_doi": ["$\\frac{ac}{bd}$"]},
             "to_chuc_thuc_hien": {"chuyen_giao_nhiem_vu": "1", "thuc_hien_nhiem_vu": "2",
                                   "bao_cao_thao_luan": "3", "ket_luan_nhan_dinh": "4"}},
        ],
    }


# ── Validator ───────────────────────────────────────────────────────────────

def test_validate_khbd_accepts_valid():
    r = validate_khbd(_valid_khbd(), ["TOAN6.PHANSO.04"], "toan")
    assert r["ok"] is True
    assert r["errors"] == []


def test_validate_khbd_rejects_bad_grounding():
    # ref không nằm trong danh mục bài → reject
    r = validate_khbd(_valid_khbd(), ["TOAN6.STP.01"], "toan")
    assert r["ok"] is False
    assert any("ngoài danh mục" in e or "khớp yêu cầu" in e for e in r["errors"])


def test_validate_khbd_requires_yccd_refs():
    khbd = _valid_khbd()
    khbd["muc_tieu"]["yccd_refs"] = []
    r = validate_khbd(khbd, ["TOAN6.PHANSO.04"], "toan")
    assert r["ok"] is False
    assert any("yccd_refs" in e for e in r["errors"])


def test_validate_khbd_rejects_empty_content():
    khbd = _valid_khbd()
    khbd["tien_trinh"][1]["noi_dung"]["cau_hoi_nhiem_vu"] = []
    r = validate_khbd(khbd, ["TOAN6.PHANSO.04"], "toan")
    assert r["ok"] is False
    assert any("cau_hoi_nhiem_vu" in e for e in r["errors"])


def test_validate_khbd_requires_hinh_thanh_kien_thuc():
    khbd = _valid_khbd()
    khbd["tien_trinh"] = [khbd["tien_trinh"][0]]  # chỉ còn mo_dau
    r = validate_khbd(khbd, ["TOAN6.PHANSO.04"], "toan")
    assert r["ok"] is False
    assert any("Hình thành kiến thức" in e for e in r["errors"])


def test_validate_khbd_warns_cliche_content():
    khbd = _valid_khbd()
    # nội dung chung chung, không số/biểu thức
    khbd["tien_trinh"][0]["noi_dung"]["cau_hoi_nhiem_vu"] = ["HS nắm được kiến thức"]
    r = validate_khbd(khbd, ["TOAN6.PHANSO.04"], "toan")
    assert any("chung chung" in w for w in r["warnings"])


def test_khbd_schema_shape():
    # schema map Phụ lục IV: muc_tieu / thiet_bi_day_hoc / tien_trinh
    assert KHBD_SCHEMA["type"] == "OBJECT"
    assert set(KHBD_SCHEMA["required"]) == {"muc_tieu", "thiet_bi_day_hoc", "tien_trinh"}


# ── Render Word ─────────────────────────────────────────────────────────────

def test_render_cv5512_produces_valid_docx_with_omml(tmp_path):
    data = render_khbd_docx(_valid_khbd(), template="cv5512")
    assert len(data) > 5000
    f = tmp_path / "khbd.docx"
    f.write_bytes(data)
    doc = Document(str(f))
    # công thức $...$ render thành phương trình Word (OMML)
    omml = doc.element.findall(".//" + qn("m:oMath"))
    assert len(omml) >= 4
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert "I. MỤC TIÊU" in full_text
    assert "III. TIẾN TRÌNH DẠY HỌC" in full_text


def test_render_school_without_template_raises():
    import pytest
    with pytest.raises(RuntimeError):
        render_khbd_docx(_valid_khbd(), template="school", template_path=None)


# ── Logic ánh xạ bài → YCCĐ (pure) ──────────────────────────────────────────

def test_candidate_yccd_covers_all_toan6_chapters():
    from scripts.map_yccd_lessons import _candidate_yccd, CHAPTER_TO_TOPICS_TOAN6
    from app.db.seed_yccd import YCCD_TOAN_6

    class _Y:
        def __init__(self, d): self.code, self.topic = d["code"], d["topic"]
    all_yccd = [_Y(d) for d in YCCD_TOAN_6]

    for ch in range(1, 10):
        cands = _candidate_yccd(ch, all_yccd)
        assert cands, f"chương {ch} không có YCCĐ ứng viên"
        topics = set(CHAPTER_TO_TOPICS_TOAN6[ch])
        assert all(c.topic in topics for c in cands)


# ── API: seed + dropdown + grounding guard ──────────────────────────────────

def test_lessons_endpoint_lists_toan6(client, make_teacher):
    _, headers = make_teacher()
    r = client.get("/api/v1/lesson-plans/lessons?grade=6&subject_code=toan", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["grade"] == 6
    # curriculum seed Toán 6 KNTT có > 40 bài
    assert len(body["lessons"]) >= 40
    assert all("curriculum_id" in l and "has_yccd" in l for l in body["lessons"])


def test_generate_rejects_unmapped_lesson(client, make_teacher):
    _, headers = make_teacher()
    lessons = client.get("/api/v1/lesson-plans/lessons?grade=6", headers=headers).json()["lessons"]
    # Bài chưa map YCCĐ (mapping là script riêng, chưa chạy ở seed) → 400 grounding guard
    unmapped = next(l for l in lessons if not l["has_yccd"])
    r = client.post("/api/v1/lesson-plans/generate",
                    json={"curriculum_id": unmapped["curriculum_id"]}, headers=headers)
    assert r.status_code == 400
    assert "ánh xạ" in r.json()["detail"]


def test_generate_rejects_missing_lesson(client, make_teacher):
    _, headers = make_teacher()
    r = client.post("/api/v1/lesson-plans/generate",
                    json={"curriculum_id": 999999}, headers=headers)
    assert r.status_code == 400


def test_lesson_plans_list_empty_initially(client, make_teacher):
    _, headers = make_teacher()
    r = client.get("/api/v1/lesson-plans", headers=headers)
    assert r.status_code == 200
    assert r.json()["total"] == 0


def test_yccd_seeded_on_startup(client):
    """Lifespan seed nạp 37 YCCĐ Toán 6."""
    async def _count():
        from app.db.session import AsyncSessionLocal
        from sqlalchemy import select, func
        from app.db.models.lesson_plan import Yccd
        async with AsyncSessionLocal() as s:
            return (await s.execute(select(func.count()).select_from(Yccd))).scalar()
    # đảm bảo lifespan đã chạy (client là context manager session-scoped)
    _ = client.get("/health")
    n = asyncio.run(_count())
    assert n >= 37
