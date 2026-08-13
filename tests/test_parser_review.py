"""
Test luồng DUYỆT CÂU — chốt người xem thứ hai trước khi vào ngân hàng.

Sau khi OCR + AI parse xong, kết quả KHÔNG vào thẳng ngân hàng mà dừng ở bảng
`questiondraft` để giáo viên sửa, tách câu bị dính, gộp câu bị vỡ, rồi mới
commit. Đây là chỗ chặn rác OCR — nếu hỏng, ngân hàng nhiễm bẩn dần mà không ai
biết, và đó là tài sản dài hạn của người dùng.

CÁCH GIEO DỮ LIỆU
Tạo draft "thật" đòi phải chạy OCR + gọi Gemini, không làm được trong CI. Nên
gieo thẳng Exam + QuestionDraft vào DB test bằng sqlite3 đồng bộ (cùng cách
fixture make_admin dùng), rồi test các endpoint HTTP bên trên. Kiểm chứng đúng
tầng đang quan tâm mà không phụ thuộc mạng.
"""

import json
import sqlite3
from datetime import datetime, timezone

import pytest

API = "/api/v1"


def _now():
    return datetime.now(timezone.utc).isoformat(sep=" ")


@pytest.fixture
def gieo_ban_nhap(client, make_teacher):
    """Factory: tạo exam + n bản nháp câu hỏi. Trả (headers, exam_id, [draft_id])."""

    def _gieo(so_cau=3, **kw):
        _, h = make_teacher()
        uid = client.get(f"{API}/auth/me", headers=h).json()["id"]

        con = sqlite3.connect("_pytest.db")
        try:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO exam (user_id, filename, status, created_at, origin,"
                " reviewed_by_user, subject_code) VALUES (?,?,?,?,?,?,?)",
                (uid, "de-kiem-tra.pdf", "needs_review", _now(), "OCR_IMPORT", 0, "toan"),
            )
            exam_id = cur.lastrowid

            draft_ids = []
            for i in range(so_cau):
                cur.execute(
                    "INSERT INTO questiondraft (exam_id, user_id, cau_num, question_order,"
                    " page_num, question_text, subject_code, question_type, topic, difficulty,"
                    " grade, chapter, answer, solution_steps, bbox_json, source_block_ids_json,"
                    " confidence, status, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        exam_id, uid, i + 1, i + 1, 1,
                        kw.get("text", f"Câu {i + 1}. Tính $x + {i}$"),
                        "toan", "TN", "Số học", "NB", 6, "Chương I. Tập hợp",
                        "A", json.dumps(["Bước 1"]),
                        json.dumps([0.1, 0.1 + i * 0.2, 0.9, 0.25 + i * 0.2]),
                        json.dumps([f"b{i}a", f"b{i}b"]),
                        0.9, "pending", _now(), _now(),
                    ),
                )
                draft_ids.append(cur.lastrowid)
            con.commit()
        finally:
            con.close()

        return h, exam_id, draft_ids

    return _gieo


# ─────────────────────────────────────────────────────────────
# Đọc bản nháp
# ─────────────────────────────────────────────────────────────

def test_lay_danh_sach_ban_nhap(client, gieo_ban_nhap):
    h, exam_id, ids = gieo_ban_nhap(so_cau=3)

    r = client.get(f"{API}/parser/{exam_id}/review", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["drafts"]) == 3
    assert {q["id"] for q in d["drafts"]} == set(ids)
    assert d["drafts"][0]["question_text"].startswith("Câu 1")


def test_ban_nhap_giu_vi_tri_tren_trang(client, gieo_ban_nhap):
    """page_num + bbox là thứ cho phép overlay lên ảnh trang gốc để đối chiếu."""
    h, exam_id, _ = gieo_ban_nhap(so_cau=2)

    q = client.get(f"{API}/parser/{exam_id}/review", headers=h).json()["drafts"][0]
    assert q["page_num"] == 1
    assert q["bbox"] and len(q["bbox"]) == 4


def test_khong_xem_duoc_ban_nhap_cua_nguoi_khac(client, gieo_ban_nhap, make_teacher):
    h, exam_id, _ = gieo_ban_nhap()
    _, hB = make_teacher()

    r = client.get(f"{API}/parser/{exam_id}/review", headers=hB)
    assert r.status_code in (403, 404), f"B xem được bản nháp của A (mã {r.status_code})"


def test_ban_nhap_cua_exam_khong_ton_tai(client, make_teacher):
    _, h = make_teacher()
    assert client.get(f"{API}/parser/999999/review", headers=h).status_code == 404


# ─────────────────────────────────────────────────────────────
# Sửa bản nháp
# ─────────────────────────────────────────────────────────────

def test_sua_noi_dung_ban_nhap(client, gieo_ban_nhap):
    h, exam_id, ids = gieo_ban_nhap()

    r = client.patch(f"{API}/parser/{exam_id}/review/questions/{ids[0]}", json={
        "question_text": "Câu 1. Nội dung đã sửa tay",
        "difficulty": "VD",
        "answer": "C",
    }, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["question_text"] == "Câu 1. Nội dung đã sửa tay"
    assert d["difficulty"] == "VD"
    assert d["answer"] == "C"


def test_sua_roi_doc_lai_van_giu(client, gieo_ban_nhap):
    h, exam_id, ids = gieo_ban_nhap()
    client.patch(f"{API}/parser/{exam_id}/review/questions/{ids[1]}",
                 json={"question_text": "Đã sửa và phải bền"}, headers=h)

    ds = client.get(f"{API}/parser/{exam_id}/review", headers=h).json()["drafts"]
    assert next(q for q in ds if q["id"] == ids[1])["question_text"] == "Đã sửa và phải bền"


def test_danh_dau_bo_ban_nhap(client, gieo_ban_nhap):
    """Câu rác từ OCR phải loại được trước khi commit."""
    h, exam_id, ids = gieo_ban_nhap()

    r = client.patch(f"{API}/parser/{exam_id}/review/questions/{ids[0]}",
                     json={"status": "rejected"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"


def test_khong_sua_duoc_ban_nhap_cua_nguoi_khac(client, gieo_ban_nhap, make_teacher):
    h, exam_id, ids = gieo_ban_nhap()
    _, hB = make_teacher()

    r = client.patch(f"{API}/parser/{exam_id}/review/questions/{ids[0]}",
                     json={"question_text": "B chiếm"}, headers=hB)
    assert r.status_code in (403, 404)

    ds = client.get(f"{API}/parser/{exam_id}/review", headers=h).json()["drafts"]
    assert all(q["question_text"] != "B chiếm" for q in ds)


# ─────────────────────────────────────────────────────────────
# Gộp câu bị vỡ
# ─────────────────────────────────────────────────────────────

def test_gop_hai_ban_nhap(client, gieo_ban_nhap):
    """OCR hay cắt một câu thành hai. Gộp phải nối nội dung và bớt đi một bản."""
    h, exam_id, ids = gieo_ban_nhap(so_cau=3)

    r = client.post(f"{API}/parser/{exam_id}/review/questions/merge",
                    json={"draft_ids": [ids[0], ids[1]]}, headers=h)
    assert r.status_code == 200, r.text

    con_lai = client.get(f"{API}/parser/{exam_id}/review", headers=h).json()["drafts"]
    assert len(con_lai) == 2, f"gộp 2 câu mà số bản nháp không giảm: {len(con_lai)}"

    gop = r.json()
    assert "Câu 1" in gop["question_text"] and "Câu 2" in gop["question_text"], (
        "nội dung hai câu không được nối vào nhau"
    )


def test_gop_mot_ban_nhap_bi_tu_choi(client, gieo_ban_nhap):
    h, exam_id, ids = gieo_ban_nhap()
    r = client.post(f"{API}/parser/{exam_id}/review/questions/merge",
                    json={"draft_ids": [ids[0]]}, headers=h)
    assert r.status_code in (400, 422)


def test_khong_gop_duoc_ban_nhap_cua_nguoi_khac(client, gieo_ban_nhap, make_teacher):
    h, exam_id, ids = gieo_ban_nhap()
    _, hB = make_teacher()

    r = client.post(f"{API}/parser/{exam_id}/review/questions/merge",
                    json={"draft_ids": ids[:2]}, headers=hB)
    assert r.status_code in (403, 404)


# ─────────────────────────────────────────────────────────────
# Commit vào ngân hàng
#
# LUỒNG THẬT: bản nháp sinh ra ở trạng thái `pending`. Commit CHỈ lấy bản ở
# trạng thái `accepted` (app/api/parser.py). Nghĩa là giáo viên phải duyệt từng
# câu — không duyệt thì không có gì vào ngân hàng. Đây chính là cơ chế chặn rác
# OCR, nên các test dưới đây luôn duyệt tường minh trước khi commit.
# ─────────────────────────────────────────────────────────────

def _duyet(client, headers, exam_id, draft_ids):
    for did in draft_ids:
        r = client.patch(f"{API}/parser/{exam_id}/review/questions/{did}",
                         json={"status": "accepted"}, headers=headers)
        assert r.status_code == 200, r.text


def test_khong_duyet_cau_nao_thi_khong_commit_duoc(client, gieo_ban_nhap):
    """Mặc định mọi bản nháp là `pending` → commit phải từ chối.

    Đây là hàng rào quan trọng nhất của bước duyệt: không ai vô tình đẩy nguyên
    kết quả OCR thô vào ngân hàng chỉ bằng một cú bấm.
    """
    h, exam_id, _ = gieo_ban_nhap(so_cau=3)

    r = client.post(f"{API}/parser/{exam_id}/review/commit", headers=h)
    assert r.status_code == 422, f"commit được khi chưa duyệt câu nào (mã {r.status_code})"
    assert client.get(f"{API}/questions", params={"my_only": "true"},
                      headers=h).json()["total"] == 0


def test_commit_dua_cau_da_duyet_vao_ngan_hang(client, gieo_ban_nhap):
    h, exam_id, ids = gieo_ban_nhap(so_cau=3)
    _duyet(client, h, exam_id, ids)

    r = client.post(f"{API}/parser/{exam_id}/review/commit", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["saved"] == 3, f"duyệt 3 câu mà lưu {r.json()['saved']}"

    assert client.get(f"{API}/questions", params={"my_only": "true"},
                      headers=h).json()["total"] == 3


def test_commit_bo_qua_cau_da_loai(client, gieo_ban_nhap):
    """Duyệt 2, loại 1 → chỉ 2 câu vào ngân hàng. Đây là toàn bộ mục đích của
    bước duyệt: chặn rác OCR."""
    h, exam_id, ids = gieo_ban_nhap(so_cau=3)
    _duyet(client, h, exam_id, ids[:2])
    client.patch(f"{API}/parser/{exam_id}/review/questions/{ids[2]}",
                 json={"status": "rejected"}, headers=h)

    d = client.post(f"{API}/parser/{exam_id}/review/commit", headers=h).json()
    assert d["saved"] == 2, f"câu đã loại vẫn vào ngân hàng: {d}"
    assert client.get(f"{API}/questions", params={"my_only": "true"},
                      headers=h).json()["total"] == 2


def test_commit_giu_noi_dung_da_sua_tay(client, gieo_ban_nhap):
    """Bản vào ngân hàng phải là bản giáo viên đã sửa, không phải bản gốc OCR."""
    h, exam_id, ids = gieo_ban_nhap(so_cau=1)
    client.patch(f"{API}/parser/{exam_id}/review/questions/{ids[0]}",
                 json={"question_text": "NOI_DUNG_GIAO_VIEN_SUA_TAY",
                       "status": "accepted"}, headers=h)

    client.post(f"{API}/parser/{exam_id}/review/commit", headers=h)

    ds = client.get(f"{API}/questions", params={"my_only": "true", "page_size": 100},
                    headers=h).json()["items"]
    assert any(q["question_text"] == "NOI_DUNG_GIAO_VIEN_SUA_TAY" for q in ds), (
        "bản vào ngân hàng không phải bản đã sửa"
    )


def test_cau_tu_duyet_mang_nhan_nguon_goc_OCR(client, gieo_ban_nhap):
    """Câu vào ngân hàng qua đường OCR phải mang nhãn nguồn gốc (Điều 44 Luật
    Công nghiệp Công nghệ số) và cờ đã-được-người-duyệt."""
    h, exam_id, ids = gieo_ban_nhap(so_cau=1)
    _duyet(client, h, exam_id, ids)
    client.post(f"{API}/parser/{exam_id}/review/commit", headers=h)

    q = client.get(f"{API}/questions", params={"my_only": "true"}, headers=h).json()["items"][0]
    assert q.get("origin") in ("OCR_IMPORT", "AI_ASSISTED", "HUMAN"), q
    assert q.get("reviewed_by_user") is True, "commit qua bước duyệt mà không đánh dấu đã duyệt"


def test_khong_commit_duoc_ban_nhap_cua_nguoi_khac(client, gieo_ban_nhap, make_teacher):
    h, exam_id, ids = gieo_ban_nhap()
    _duyet(client, h, exam_id, ids)
    _, hB = make_teacher()

    r = client.post(f"{API}/parser/{exam_id}/review/commit", headers=hB)
    assert r.status_code in (403, 404)

    assert client.get(f"{API}/questions", params={"my_only": "true"},
                      headers=hB).json()["total"] == 0
