"""
Test hành vi cho `classes` + `assignments` — TRƯỚC ĐÂY 0 test HTTP.

TẠI SAO VÙNG NÀY ĐÁNG PHỦ TEST NHẤT LÚC NÀY
Đây là nền móng của tính năng sổ điểm TT22/27 sắp làm. Audit 2026-08-11 kết
luận: quan hệ học sinh–lớp là many-to-many ĐÚNG về cấu trúc (bảng nối
`classmember` có UNIQUE + is_active), nhưng đường vào dữ liệu đã bị cắt —
`main.py` ép mọi role về 'teacher' mỗi lần boot nên không tạo được học sinh nữa.

Vì vậy test ở đây phủ phần CÒN SỐNG (giáo viên tạo lớp, giao bài) và ghi nhận
rõ phần đã chết, để khi dựng lớp danh tính học sinh mới thì biết mình đang xây
lên cái gì.
"""

API = "/api/v1"


def _tao_lop(client, headers, **kw):
    payload = {"name": "Lớp 6A1", "subject_code": "toan", "grade": 6}
    payload.update(kw)
    r = client.post(f"{API}/classes", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _giao_bai(client, headers, class_id, **kw):
    payload = {"class_id": class_id, "title": "Bài tập về nhà số 1"}
    payload.update(kw)
    r = client.post(f"{API}/assignments", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# ─────────────────────────────────────────────────────────────
# Lớp học
# ─────────────────────────────────────────────────────────────

def test_tao_va_doc_lai_lop(client, make_teacher):
    _, h = make_teacher()
    lop = _tao_lop(client, h)

    assert lop["name"] == "Lớp 6A1"
    assert lop["grade"] == 6
    assert lop["code"], "lớp phải có mã tham gia"
    assert len(lop["code"]) <= 10

    doc = client.get(f"{API}/classes/{lop['id']}", headers=h)
    assert doc.status_code == 200
    assert doc.json()["id"] == lop["id"]


def test_ma_lop_khong_trung_nhau(client, make_teacher):
    _, h = make_teacher()
    ma = {_tao_lop(client, h)["code"] for _ in range(5)}
    assert len(ma) == 5, "mã lớp bị trùng — học sinh sẽ vào nhầm lớp"


def test_danh_sach_lop_chi_hien_lop_cua_minh(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    lop_a = _tao_lop(client, hA, name="Lớp của A")
    _tao_lop(client, hB, name="Lớp của B")

    ds = client.get(f"{API}/classes", headers=hA).json()
    assert lop_a["id"] in {c["id"] for c in ds}
    assert "Lớp của B" not in {c["name"] for c in ds}


def test_khong_doc_duoc_lop_cua_giao_vien_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    lop = _tao_lop(client, hA)

    r = client.get(f"{API}/classes/{lop['id']}", headers=hB)
    assert r.status_code in (403, 404), f"B đọc được lớp của A (mã {r.status_code})"


def test_khong_xem_duoc_danh_sach_thanh_vien_lop_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    lop = _tao_lop(client, hA)

    r = client.get(f"{API}/classes/{lop['id']}/members", headers=hB)
    assert r.status_code in (403, 404)


def test_lop_moi_chua_co_thanh_vien(client, make_teacher):
    _, h = make_teacher()
    lop = _tao_lop(client, h)
    assert client.get(f"{API}/classes/{lop['id']}/members", headers=h).json() == []


# ─────────────────────────────────────────────────────────────
# Giao bài tập
# ─────────────────────────────────────────────────────────────

def test_giao_bai_va_liet_ke_theo_lop(client, make_teacher):
    _, h = make_teacher()
    lop = _tao_lop(client, h)
    bai = _giao_bai(client, h, lop["id"])

    assert bai["title"] == "Bài tập về nhà số 1"
    assert bai["class_id"] == lop["id"]

    ds = client.get(f"{API}/assignments", params={"class_id": lop["id"]}, headers=h).json()
    assert bai["id"] in {a["id"] for a in ds}


def test_khong_giao_bai_vao_lop_cua_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    lop_a = _tao_lop(client, hA)

    r = client.post(f"{API}/assignments",
                    json={"class_id": lop_a["id"], "title": "B chen ngang"}, headers=hB)
    assert r.status_code in (403, 404), "B giao được bài vào lớp của A"


def test_khong_doc_duoc_bai_tap_cua_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    lop = _tao_lop(client, hA)
    bai = _giao_bai(client, hA, lop["id"])

    assert client.get(f"{API}/assignments/{bai['id']}", headers=hB).status_code in (403, 404)


def test_sua_bai_tap_dung_method_patch(client, make_teacher):
    """Backend khai báo PATCH. FE từng gọi PUT → 405; wrapper hỏng đó đã gỡ
    2026-08-12. Test này chốt lại method đúng để khỏi lặp lại."""
    _, h = make_teacher()
    lop = _tao_lop(client, h)
    bai = _giao_bai(client, h, lop["id"])

    assert client.put(f"{API}/assignments/{bai['id']}",
                      json={"title": "x"}, headers=h).status_code == 405

    r = client.patch(f"{API}/assignments/{bai['id']}", json={"title": "Tên mới"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Tên mới"


def test_xoa_bai_tap(client, make_teacher):
    _, h = make_teacher()
    lop = _tao_lop(client, h)
    bai = _giao_bai(client, h, lop["id"])

    assert client.delete(f"{API}/assignments/{bai['id']}", headers=h).status_code == 204
    assert client.get(f"{API}/assignments/{bai['id']}", headers=h).status_code == 404


def test_xoa_lop_thi_xoa_luon_bai_tap(client, make_teacher):
    """CASCADE — bài tập mồ côi sẽ hiện lơ lửng ở màn hình khác."""
    _, h = make_teacher()
    lop = _tao_lop(client, h)
    bai = _giao_bai(client, h, lop["id"])

    assert client.delete(f"{API}/classes/{lop['id']}", headers=h).status_code == 204
    assert client.get(f"{API}/assignments/{bai['id']}", headers=h).status_code in (403, 404)


# ─────────────────────────────────────────────────────────────
# Bài nộp — GHI NHẬN hiện trạng: bảng không còn nguồn dữ liệu
# ─────────────────────────────────────────────────────────────

def test_bai_nop_cua_bai_tap_luon_rong(client, make_teacher):
    """Endpoint đọc bài nộp phía giáo viên vẫn còn, nhưng LUÔN trả rỗng.

    Lý do: các endpoint để học sinh nộp bài đã gỡ 2026-08-12 (chỉ app
    mathplay-mobile đã ngừng mới gọi), và không tạo được tài khoản học sinh nữa.

    Test này KHÔNG phải để khẳng định "rỗng là đúng" — nó ghi lại hiện trạng.
    Khi làm sổ điểm TT22/27 và có nguồn dữ liệu mới, test này sẽ đỏ và nhắc
    cập nhật.
    """
    _, h = make_teacher()
    lop = _tao_lop(client, h)
    bai = _giao_bai(client, h, lop["id"])

    r = client.get(f"{API}/submissions/assignment/{bai['id']}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_khong_xem_duoc_bai_nop_cua_lop_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    lop = _tao_lop(client, hA)
    bai = _giao_bai(client, hA, lop["id"])

    assert client.get(f"{API}/submissions/assignment/{bai['id']}", headers=hB).status_code in (403, 404)


def test_endpoint_danh_cho_hoc_sinh_da_go_han(client, make_teacher):
    """Chốt lại việc gỡ 2026-08-12 để không ai vô tình thêm lại.

    Chấp nhận cả 422: đường như `/assignments/for-student` bị route
    `GET /assignments/{assignment_id}` bắt rồi fail validate vì "for-student"
    không phải số nguyên. Đó vẫn là "endpoint không tồn tại" — một endpoint
    còn sống sẽ trả 200/403, không bao giờ trả 422 cho request hợp lệ.
    """
    _, h = make_teacher()
    for method, path in [
        ("post", f"{API}/classes/join"),
        ("get", f"{API}/classes/my/enrolled"),
        ("get", f"{API}/assignments/for-student"),
        ("post", f"{API}/submissions"),
        ("get", f"{API}/submissions/my"),
    ]:
        r = getattr(client, method)(path, headers=h, **({"json": {}} if method == "post" else {}))
        assert r.status_code in (404, 405, 422), (
            f"{method.upper()} {path} vẫn còn sống (mã {r.status_code}) — "
            f"endpoint dành cho học sinh đã gỡ ở commit d6e1177"
        )
