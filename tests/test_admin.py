"""
Test hành vi cho router `admin` — 9 endpoint, TRƯỚC ĐÂY 0 test HTTP.

TẠI SAO VÙNG NÀY NHẠY CẢM NHẤT
Đây là các endpoint xem/sửa/xóa dữ liệu của MỌI người dùng, chặn bằng
`get_current_active_superuser` (role == "admin"). Nếu cổng đó hỏng khi refactor,
một giáo viên bất kỳ đọc được toàn bộ ngân hàng câu hỏi của người khác và đổi
được quyền tài khoản.

`tests/test_auth_contract.py` chỉ khẳng định các đường này chặn KHÁCH VÔ DANH.
File này khẳng định thêm: chặn cả người ĐÃ ĐĂNG NHẬP nhưng KHÔNG phải quản trị.
"""

API = "/api/v1"

DUONG_ADMIN = [
    ("get", "/admin/stats"),
    ("get", "/admin/users"),
    ("get", "/admin/questions"),
    ("get", "/admin/questions/duplicates"),
]


# ─────────────────────────────────────────────────────────────
# Cổng quyền — bất biến quan trọng nhất
# ─────────────────────────────────────────────────────────────

def test_giao_vien_thuong_khong_vao_duoc_dat_ca_duong_admin(client, make_teacher):
    _, h = make_teacher()
    for method, duong in DUONG_ADMIN:
        r = getattr(client, method)(f"{API}{duong}", headers=h)
        assert r.status_code == 403, (
            f"{method.upper()} {duong} cho giáo viên thường vào (mã {r.status_code}) — "
            f"cổng superuser hỏng"
        )


def test_giao_vien_thuong_khong_sua_duoc_quyen_nguoi_khac(client, make_teacher):
    """Leo thang đặc quyền: tự phong mình làm admin."""
    email_a, hA = make_teacher()
    _, hB = make_teacher()

    ds = client.get(f"{API}/admin/users", headers=hB)
    assert ds.status_code == 403

    # Thử sửa thẳng bằng id đoán được
    r = client.patch(f"{API}/admin/users/1", json={"role": "admin"}, headers=hB)
    assert r.status_code == 403


def test_quan_tri_vao_duoc(client, make_admin):
    _, ha = make_admin()
    for method, duong in DUONG_ADMIN:
        r = getattr(client, method)(f"{API}{duong}", headers=ha)
        assert r.status_code == 200, f"{duong} → {r.status_code}: {r.text[:200]}"


# ─────────────────────────────────────────────────────────────
# Thống kê + danh sách
# ─────────────────────────────────────────────────────────────

def test_stats_tra_ve_du_truong(client, make_admin):
    _, ha = make_admin()
    d = client.get(f"{API}/admin/stats", headers=ha).json()
    for truong in ("total_users", "total_questions", "total_exams"):
        assert truong in d, f"thiếu trường {truong}"
        assert isinstance(d[truong], int)


def test_danh_sach_nguoi_dung_thay_moi_nguoi(client, make_teacher, make_admin):
    """Khác với router thường: admin PHẢI thấy dữ liệu của tất cả."""
    email_gv, _ = make_teacher()
    _, ha = make_admin()

    d = client.get(f"{API}/admin/users", params={"limit": 200}, headers=ha).json()
    emails = {u["email"] for u in d["items"]}
    assert email_gv in emails, "admin không thấy giáo viên vừa tạo"
    assert d["total"] >= 2


def test_tim_nguoi_dung_theo_tu_khoa(client, make_teacher, make_admin):
    email_gv, _ = make_teacher()
    _, ha = make_admin()

    d = client.get(f"{API}/admin/users",
                   params={"search": email_gv.split("@")[0]}, headers=ha).json()
    assert email_gv in {u["email"] for u in d["items"]}


def test_danh_sach_cau_hoi_gom_cua_moi_giao_vien(client, make_teacher, make_admin):
    _, hA = make_teacher()
    client.post(f"{API}/questions/bulk", json={"questions": [
        {"question_text": "ADMIN_THAY_DUOC_CAU_NAY", "subject_code": "toan",
         "question_type": "TN", "difficulty": "NB", "grade": 6, "answer": "A"},
    ]}, headers=hA)

    _, ha = make_admin()
    d = client.get(f"{API}/admin/questions", params={"page_size": 100}, headers=ha).json()
    assert "ADMIN_THAY_DUOC_CAU_NAY" in {q["question_text"] for q in d["items"]}


# ─────────────────────────────────────────────────────────────
# Sửa / xóa
# ─────────────────────────────────────────────────────────────

def test_quan_tri_doi_duoc_quyen_va_trang_thai_tai_khoan(client, make_teacher, make_admin):
    email_gv, _ = make_teacher()
    _, ha = make_admin()

    ds = client.get(f"{API}/admin/users", params={"search": email_gv.split("@")[0]},
                    headers=ha).json()
    uid = next(u["id"] for u in ds["items"] if u["email"] == email_gv)

    r = client.patch(f"{API}/admin/users/{uid}", json={"is_active": False}, headers=ha)
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is False


def test_quan_tri_xoa_duoc_cau_hoi_cua_nguoi_khac(client, make_teacher, make_admin):
    _, hA = make_teacher()
    client.post(f"{API}/questions/bulk", json={"questions": [
        {"question_text": "CAU_SE_BI_ADMIN_XOA", "subject_code": "toan",
         "question_type": "TN", "difficulty": "NB", "grade": 6, "answer": "A"},
    ]}, headers=hA)
    qid = next(q["id"] for q in client.get(
        f"{API}/questions", params={"my_only": "true", "page_size": 100},
        headers=hA).json()["items"] if q["question_text"] == "CAU_SE_BI_ADMIN_XOA")

    _, ha = make_admin()
    assert client.delete(f"{API}/admin/questions/{qid}", headers=ha).status_code in (200, 204)

    # Chủ cũ không còn thấy câu đó
    con_lai = {q["question_text"] for q in client.get(
        f"{API}/questions", params={"my_only": "true", "page_size": 100},
        headers=hA).json()["items"]}
    assert "CAU_SE_BI_ADMIN_XOA" not in con_lai


def test_giao_vien_thuong_khong_xoa_duoc_qua_duong_admin(client, make_teacher):
    _, hA = make_teacher()
    client.post(f"{API}/questions/bulk", json={"questions": [
        {"question_text": "KHONG_DUOC_XOA_QUA_ADMIN", "subject_code": "toan",
         "question_type": "TN", "difficulty": "NB", "grade": 6, "answer": "A"},
    ]}, headers=hA)
    qid = next(q["id"] for q in client.get(
        f"{API}/questions", params={"my_only": "true", "page_size": 100},
        headers=hA).json()["items"] if q["question_text"] == "KHONG_DUOC_XOA_QUA_ADMIN")

    _, hB = make_teacher()
    assert client.delete(f"{API}/admin/questions/{qid}", headers=hB).status_code == 403
    # Câu vẫn còn
    assert client.get(f"{API}/questions/{qid}", headers=hA).status_code == 200


def test_doi_hien_thi_hang_loat_qua_duong_admin(client, make_teacher, make_admin):
    _, hA = make_teacher()
    client.post(f"{API}/questions/bulk", json={"questions": [
        {"question_text": "CAU_DOI_HIEN_THI", "subject_code": "toan",
         "question_type": "TN", "difficulty": "NB", "grade": 6, "answer": "A"},
    ]}, headers=hA)
    qid = next(q["id"] for q in client.get(
        f"{API}/questions", params={"my_only": "true", "page_size": 100},
        headers=hA).json()["items"] if q["question_text"] == "CAU_DOI_HIEN_THI")

    _, ha = make_admin()
    r = client.patch(f"{API}/admin/questions/bulk-visibility",
                     json={"question_ids": [qid], "is_public": True}, headers=ha)
    assert r.status_code == 200, r.text
