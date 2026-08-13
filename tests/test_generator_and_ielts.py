"""
Test cho `generator` (6 endpoint) và cụm IELTS (4 router) — TRƯỚC ĐÂY gần như
không có test HTTP nào.

`generator` mới chỉ có `exam-matrix` được phủ (tests/test_teacher_pivot_features.py).
Cụm IELTS có 0 test, trong khi `ielts_parser.py` VỪA được refactor ở commit
2174525 (gỡ import từ api.parser) — đúng chỗ cần lưới an toàn nhất.

CHIẾN LƯỢC: các endpoint gọi Gemini KHÔNG chạy được trong CI (không có API key
thật). Nên ở đây test những gì kiểm chứng được mà không cần mạng:
  - hợp đồng đầu vào (validate, chặn giá trị vô lý)
  - cổng quyền + cách ly quyền sở hữu
  - đường ráp đề từ ngân hàng (SQL thuần, KHÔNG gọi AI)
Phần thực sự gọi Gemini để dành cho test tích hợp chạy tay.
"""

API = "/api/v1"


def _nap_ngan_hang(client, headers, n=8, grade=6, chapter="Chương I. Tập hợp"):
    """Nạp n câu vào ngân hàng để ráp đề theo ma trận."""
    qs = [
        {"question_text": f"Câu ngân hàng {i}: $x + {i} = 0$", "subject_code": "toan",
         "question_type": "TN", "difficulty": "NB" if i % 2 else "TH",
         "grade": grade, "chapter": chapter, "answer": "A"}
        for i in range(n)
    ]
    r = client.post(f"{API}/questions/bulk", json={"questions": qs}, headers=headers)
    assert r.status_code in (200, 201), r.text


# ─────────────────────────────────────────────────────────────
# Ma trận đề — đường KHÔNG gọi AI, chạy được đầy đủ trong CI
# ─────────────────────────────────────────────────────────────

def test_rap_de_theo_ma_tran_tu_ngan_hang(client, make_teacher):
    """Ráp đề theo ma trận là SQL thuần (lọc metadata), không đụng embedding."""
    _, h = make_teacher()
    _nap_ngan_hang(client, h)

    r = client.post(f"{API}/generate/exam-matrix", json={
        "title": "Đề giữa kỳ I",
        "subject_code": "toan",
        "grade": 6,
        "cells": [{"chapter": "Chương I. Tập hợp", "counts": {"NB": 2}}],
    }, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    # ExamMatrixResponse: exam_id + question_count + deficits (KHÔNG trả câu hỏi)
    assert d["question_count"] == 2, f"ma trận lấy sai số câu: {d}"
    assert d["exam_id"] > 0
    assert not d["deficits"], f"ngân hàng đủ câu mà vẫn báo thiếu: {d['deficits']}"


def test_ma_tran_thieu_cau_va_khong_cho_phep_thieu_thi_bao_loi(client, make_teacher):
    _, h = make_teacher()
    _nap_ngan_hang(client, h, n=2)

    r = client.post(f"{API}/generate/exam-matrix", json={
        "title": "Đề đòi quá nhiều",
        "subject_code": "toan", "grade": 6,
        "cells": [{"chapter": "Chương I. Tập hợp", "counts": {"NB": 40}}],
        "allow_partial": False,
    }, headers=h)
    assert r.status_code == 400, f"thiếu câu mà vẫn tạo đề (mã {r.status_code})"


def test_ma_tran_khong_lay_cau_cua_giao_vien_khac(client, make_teacher):
    """Cách ly: ma trận của B không được rút câu từ ngân hàng của A."""
    _, hA = make_teacher()
    _, hB = make_teacher()
    _nap_ngan_hang(client, hA, n=10)

    r = client.post(f"{API}/generate/exam-matrix", json={
        "title": "B rút câu của A",
        "subject_code": "toan", "grade": 6,
        "cells": [{"chapter": "Chương I. Tập hợp", "counts": {"NB": 3}}],
        "allow_partial": False,
    }, headers=hB)
    assert r.status_code == 400, "ngân hàng của B rỗng mà vẫn ráp được đề"


def test_ma_tran_tu_choi_lop_ngoai_pham_vi(client, make_teacher):
    _, h = make_teacher()
    r = client.post(f"{API}/generate/exam-matrix", json={
        "title": "Lớp 99", "subject_code": "toan", "grade": 99,
        "cells": [{"chapter": "X", "counts": {"NB": 1}}],
    }, headers=h)
    assert r.status_code == 422


def test_ma_tran_tu_choi_danh_sach_o_rong(client, make_teacher):
    _, h = make_teacher()
    r = client.post(f"{API}/generate/exam-matrix", json={
        "title": "Không ô nào", "subject_code": "toan", "grade": 6, "cells": [],
    }, headers=h)
    assert r.status_code == 422


# ─────────────────────────────────────────────────────────────
# Lưu đề đã sinh — không gọi AI
# ─────────────────────────────────────────────────────────────

def test_luu_cau_da_sinh_thanh_de(client, make_teacher):
    """Hợp đồng trường: schema là `question`/`type`, KHÔNG phải
    `question_text`/`question_type`. Đã từng sai chỗ này."""
    _, h = make_teacher()

    r = client.post(f"{API}/generate/save-as-exam", json={
        "title": "Đề AI sinh",
        "questions": [
            {"question": "Tính $2+2$", "type": "TN", "topic": "Số học",
             "difficulty": "NB", "answer": "4", "solution_steps": ["2+2=4"]},
        ],
    }, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["question_count"] == 1
    assert d["exam_id"] > 0

    # Đề vừa lưu phải hiện trong lịch sử
    lich_su = client.get(f"{API}/parser/history", headers=h).json()
    assert d["exam_id"] in {e["id"] for e in lich_su["items"]}


def test_luu_de_tu_choi_tieu_de_rong(client, make_teacher):
    _, h = make_teacher()
    r = client.post(f"{API}/generate/save-as-exam",
                    json={"title": "", "questions": []}, headers=h)
    assert r.status_code == 422


def test_de_da_luu_khong_lo_sang_giao_vien_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()

    d = client.post(f"{API}/generate/save-as-exam", json={
        "title": "ĐỀ RIÊNG CỦA A",
        "questions": [{"question": "x", "type": "TN", "answer": "A"}],
    }, headers=hA).json()

    ds_b = client.get(f"{API}/parser/history", headers=hB).json()
    assert d["exam_id"] not in {e["id"] for e in ds_b["items"]}
    assert "ĐỀ RIÊNG CỦA A" not in {e.get("filename", "") for e in ds_b["items"]}


# ─────────────────────────────────────────────────────────────
# Hợp đồng đầu vào của các endpoint sinh câu bằng AI
# (kiểm tra validate — KHÔNG chạm mạng)
# ─────────────────────────────────────────────────────────────

def test_sinh_cau_tu_choi_so_luong_vo_ly(client, make_teacher):
    _, h = make_teacher()
    for count in (0, -1, 999):
        r = client.post(f"{API}/generate", json={"subject_code": "toan", "count": count},
                        headers=h)
        assert r.status_code == 422, f"count={count} lọt qua validate"


def test_sinh_tu_mo_ta_tu_choi_prompt_qua_ngan(client, make_teacher):
    _, h = make_teacher()
    r = client.post(f"{API}/generate/from-prompt", json={"prompt": "abc"}, headers=h)
    assert r.status_code == 422


# ─────────────────────────────────────────────────────────────
# IELTS — cổng quyền và hợp đồng đầu vào
# ─────────────────────────────────────────────────────────────

def test_ielts_liet_ke_section_nghe_cua_quiz_khong_ton_tai(client, make_teacher):
    _, h = make_teacher()
    r = client.get(f"{API}/parser/ielts/999999/listening-sections", headers=h)
    assert r.status_code == 404


def test_ielts_khong_xem_duoc_section_cua_quiz_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    quiz = client.post(f"{API}/quizzes", json={"name": "IELTS của A"}, headers=hA).json()

    r = client.get(f"{API}/parser/ielts/{quiz['id']}/listening-sections", headers=hB)
    assert r.status_code in (403, 404), f"B xem được section của A (mã {r.status_code})"


def test_ielts_gan_audio_tu_choi_file_khong_phai_am_thanh(client, make_teacher):
    _, h = make_teacher()
    quiz = client.post(f"{API}/quizzes", json={"name": "IELTS test audio"}, headers=h).json()

    r = client.post(
        f"{API}/parser/ielts/{quiz['id']}/audio",
        data={"section_title": "Section 1"},
        files={"file": ("virus.exe", b"MZ\x90\x00", "application/octet-stream")},
        headers=h,
    )
    assert r.status_code in (400, 415, 422), (
        f"nhận file không phải âm thanh (mã {r.status_code})"
    )


def test_ielts_sinh_de_tu_choi_template_la(client, make_teacher):
    _, h = make_teacher()
    r = client.post(f"{API}/generate/ielts-exam",
                    json={"template": "khong_ton_tai", "name": "X"}, headers=h)
    assert r.status_code == 422


def test_ielts_sinh_de_tu_choi_ten_rong(client, make_teacher):
    _, h = make_teacher()
    r = client.post(f"{API}/generate/ielts-exam",
                    json={"template": "full_test", "name": ""}, headers=h)
    assert r.status_code == 422


def test_ielts_parse_tu_choi_file_khong_phai_tai_lieu(client, make_teacher):
    _, h = make_teacher()
    r = client.post(f"{API}/parser/parse-ielts",
                    files={"file": ("a.exe", b"MZ\x90\x00", "application/octet-stream")},
                    headers=h)
    assert r.status_code in (400, 415, 422), (
        f"luồng IELTS nhận file thực thi (mã {r.status_code})"
    )


def test_ielts_writing_grades_cua_luot_lam_khong_ton_tai(client, make_teacher):
    _, h = make_teacher()
    assert client.get(f"{API}/quiz-attempts/999999/writing-grades", headers=h).status_code == 404


# ─────────────────────────────────────────────────────────────
# ielts_parser sau refactor 2174525 — canh gác kiến trúc
# ─────────────────────────────────────────────────────────────

def test_ielts_parser_khong_import_nguoc_len_api_parser():
    """Chốt lại kết quả gỡ coupling ở commit 2174525.

    Trước đó `ielts_parser` import _publish_progress + _background_tasks (tên
    private + biến trạng thái toàn cục) từ `api/parser.py`. Nếu ai đó thêm lại,
    test này đỏ ngay.
    """
    import ast
    import pathlib

    cay = ast.parse(pathlib.Path("app/api/ielts_parser.py").read_text(encoding="utf-8"))
    nguon = []
    for node in ast.walk(cay):
        if isinstance(node, ast.ImportFrom) and node.module:
            nguon.append(node.module)
        elif isinstance(node, ast.Import):
            nguon += [a.name for a in node.names]

    assert "app.api.parser" not in nguon, (
        "ielts_parser lại import từ api.parser — coupling đã gỡ nay quay lại"
    )
