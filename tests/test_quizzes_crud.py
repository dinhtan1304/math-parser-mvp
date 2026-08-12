"""
Test hành vi cho router `quizzes` — 20 endpoint, 863 dòng, TRƯỚC ĐÂY 0 test HTTP.

TẠI SAO FILE NÀY QUAN TRỌNG
Đây là bề mặt API lớn nhất chưa được phủ (audit 2026-08-11). Bất biến quan
trọng nhất được test ở đây là CÁCH LY QUYỀN SỞ HỮU: giáo viên A không được
đọc/sửa/xóa đề của giáo viên B. Đó là invariant dễ vỡ nhất khi refactor và gây
hậu quả nặng nhất khi vỡ — mà `tests/test_auth_contract.py` KHÔNG bắt được, vì
ở đó mọi request đều có token hợp lệ, chỉ là của sai người.
"""

API = "/api/v1"


# ─────────────────────────────────────────────────────────────
# Trợ giúp
# ─────────────────────────────────────────────────────────────

def _tao_de(client, headers, **kw):
    payload = {"name": "Đề kiểm tra 15 phút", "subject_code": "toan", "grade": 6}
    payload.update(kw)
    r = client.post(f"{API}/quizzes", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _cau_hoi(**kw):
    q = {
        "type": "multiple_choice",
        "question_text": "2 + 2 = ?",
        "choices": [
            {"key": "A", "text": "3", "is_correct": False},
            {"key": "B", "text": "4", "is_correct": True},
        ],
        "answer": "B",
        "points": 1,
    }
    q.update(kw)
    return q


# ─────────────────────────────────────────────────────────────
# Vòng đời cơ bản
# ─────────────────────────────────────────────────────────────

def test_tao_va_doc_lai_de(client, make_teacher):
    _, h = make_teacher()
    de = _tao_de(client, h)

    assert de["name"] == "Đề kiểm tra 15 phút"
    assert de["code"].startswith("QUIZ-"), "mã chia sẻ phải có tiền tố QUIZ-"
    assert de["status"] == "draft", "đề mới phải ở trạng thái nháp"
    assert de["question_count"] == 0

    doc = client.get(f"{API}/quizzes/{de['id']}", headers=h)
    assert doc.status_code == 200
    assert doc.json()["id"] == de["id"]


def test_ma_de_khong_trung_nhau(client, make_teacher):
    _, h = make_teacher()
    ma = {_tao_de(client, h)["code"] for _ in range(5)}
    assert len(ma) == 5, "mã đề bị trùng — link chia sẻ sẽ trỏ nhầm đề"


def test_sua_de(client, make_teacher):
    _, h = make_teacher()
    de = _tao_de(client, h)

    r = client.patch(f"{API}/quizzes/{de['id']}", json={"name": "Tên mới", "grade": 9}, headers=h)
    assert r.status_code == 200
    assert r.json()["name"] == "Tên mới"
    assert r.json()["grade"] == 9


def test_xoa_de(client, make_teacher):
    _, h = make_teacher()
    de = _tao_de(client, h)

    assert client.delete(f"{API}/quizzes/{de['id']}", headers=h).status_code == 204
    assert client.get(f"{API}/quizzes/{de['id']}", headers=h).status_code == 404


def test_danh_sach_de_chi_hien_de_cua_minh(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    de_a = _tao_de(client, hA, name="Của A")
    _tao_de(client, hB, name="Của B")

    ds = client.get(f"{API}/quizzes", headers=hA).json()
    ids = {it["id"] for it in ds["items"]}
    assert de_a["id"] in ids
    ten = {it["name"] for it in ds["items"]}
    assert "Của B" not in ten, "đề của giáo viên khác lọt vào danh sách"


# ─────────────────────────────────────────────────────────────
# CÁCH LY QUYỀN SỞ HỮU — bất biến quan trọng nhất
# ─────────────────────────────────────────────────────────────

def test_khong_doc_duoc_de_nhap_cua_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    de = _tao_de(client, hA)

    r = client.get(f"{API}/quizzes/{de['id']}", headers=hB)
    assert r.status_code in (403, 404), f"B đọc được đề nháp của A (mã {r.status_code})"


def test_khong_sua_duoc_de_cua_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    de = _tao_de(client, hA)

    r = client.patch(f"{API}/quizzes/{de['id']}", json={"name": "B chiếm"}, headers=hB)
    assert r.status_code in (403, 404)

    # Đề của A không bị đổi
    assert client.get(f"{API}/quizzes/{de['id']}", headers=hA).json()["name"] != "B chiếm"


def test_khong_xoa_duoc_de_cua_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    de = _tao_de(client, hA)

    r = client.delete(f"{API}/quizzes/{de['id']}", headers=hB)
    assert r.status_code in (403, 404)
    assert client.get(f"{API}/quizzes/{de['id']}", headers=hA).status_code == 200


def test_khong_them_cau_vao_de_cua_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    de = _tao_de(client, hA)

    r = client.post(f"{API}/quizzes/{de['id']}/questions", json=_cau_hoi(), headers=hB)
    assert r.status_code in (403, 404)


# ─────────────────────────────────────────────────────────────
# Câu hỏi trong đề
# ─────────────────────────────────────────────────────────────

def test_them_liet_ke_sua_xoa_cau_hoi(client, make_teacher):
    _, h = make_teacher()
    de = _tao_de(client, h)

    them = client.post(f"{API}/quizzes/{de['id']}/questions", json=_cau_hoi(), headers=h)
    assert them.status_code == 201, them.text
    qid = them.json()["id"]

    ds = client.get(f"{API}/quizzes/{de['id']}/questions", headers=h).json()
    assert len(ds) == 1
    assert ds[0]["question_text"] == "2 + 2 = ?"

    sua = client.patch(
        f"{API}/quizzes/{de['id']}/questions/{qid}",
        json={"question_text": "3 + 3 = ?"}, headers=h,
    )
    assert sua.status_code == 200
    assert sua.json()["question_text"] == "3 + 3 = ?"

    assert client.delete(f"{API}/quizzes/{de['id']}/questions/{qid}", headers=h).status_code == 204
    assert client.get(f"{API}/quizzes/{de['id']}/questions", headers=h).json() == []


def test_question_count_cap_nhat_theo_so_cau(client, make_teacher):
    """question_count là trường denormalize — dễ lệch khi refactor."""
    _, h = make_teacher()
    de = _tao_de(client, h)

    for i in range(3):
        client.post(f"{API}/quizzes/{de['id']}/questions",
                    json=_cau_hoi(question_text=f"Câu {i}"), headers=h)

    assert client.get(f"{API}/quizzes/{de['id']}", headers=h).json()["question_count"] == 3


def test_them_hang_loat_cau_hoi(client, make_teacher):
    _, h = make_teacher()
    de = _tao_de(client, h)

    r = client.post(
        f"{API}/quizzes/{de['id']}/batch-questions",
        json={"questions": [_cau_hoi(question_text=f"Câu {i}") for i in range(4)]},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert len(r.json()) == 4


def test_xoa_de_thi_xoa_luon_cau_hoi(client, make_teacher):
    """CASCADE: xóa đề mà bỏ lại câu hỏi mồ côi là rò rỉ dữ liệu."""
    _, h = make_teacher()
    de = _tao_de(client, h)
    client.post(f"{API}/quizzes/{de['id']}/questions", json=_cau_hoi(), headers=h)

    client.delete(f"{API}/quizzes/{de['id']}", headers=h)
    assert client.get(f"{API}/quizzes/{de['id']}/questions", headers=h).status_code in (403, 404)


# ─────────────────────────────────────────────────────────────
# Lý thuyết
# ─────────────────────────────────────────────────────────────

def test_them_va_xoa_ly_thuyet(client, make_teacher):
    _, h = make_teacher()
    de = _tao_de(client, h)

    r = client.post(
        f"{API}/quizzes/{de['id']}/theories",
        json={"title": "Hằng đẳng thức",
              "sections": [{"order": 1, "content": "$(a+b)^2 = a^2+2ab+b^2$"}]},
        headers=h,
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]

    ds = client.get(f"{API}/quizzes/{de['id']}/theories", headers=h).json()
    assert len(ds) == 1 and ds[0]["title"] == "Hằng đẳng thức"

    assert client.delete(f"{API}/quizzes/{de['id']}/theories/{tid}", headers=h).status_code == 204
    assert client.get(f"{API}/quizzes/{de['id']}/theories", headers=h).json() == []


# ─────────────────────────────────────────────────────────────
# Chia sẻ theo mã + cổng "đã xuất bản"
# ─────────────────────────────────────────────────────────────

def test_de_nhap_khong_mo_duoc_bang_ma_boi_nguoi_la(client, make_teacher):
    _, hA = make_teacher()
    de = _tao_de(client, hA)

    r = client.get(f"{API}/quizzes/by-code/{de['code']}")  # không token
    assert r.status_code == 403, "đề NHÁP bị lộ qua link chia sẻ"


def test_de_da_xuat_ban_mo_duoc_khong_can_dang_nhap(client, make_teacher):
    _, hA = make_teacher()
    de = _tao_de(client, hA)
    client.patch(f"{API}/quizzes/{de['id']}", json={"status": "published"}, headers=hA)

    r = client.get(f"{API}/quizzes/by-code/{de['code']}")
    assert r.status_code == 200, r.text
    assert r.json()["code"] == de["code"]


def test_chu_de_van_xem_duoc_de_nhap_cua_minh_bang_ma(client, make_teacher):
    _, hA = make_teacher()
    de = _tao_de(client, hA)
    assert client.get(f"{API}/quizzes/by-code/{de['code']}", headers=hA).status_code == 200


def test_ma_de_khong_ton_tai_tra_404(client, make_teacher):
    _, h = make_teacher()
    assert client.get(f"{API}/quizzes/by-code/QUIZ-KHONGCO", headers=h).status_code == 404


# ─────────────────────────────────────────────────────────────
# Thông tin trước khi xóa + xuất file
# ─────────────────────────────────────────────────────────────

def test_delete_info_dem_dung_truoc_khi_xoa(client, make_teacher):
    """FE hiện hộp xác nhận dựa vào số liệu này — sai số là người dùng xóa nhầm."""
    _, h = make_teacher()
    de = _tao_de(client, h)
    client.post(f"{API}/quizzes/{de['id']}/questions", json=_cau_hoi(), headers=h)
    client.post(f"{API}/quizzes/{de['id']}/theories",
                json={"title": "LT", "sections": [{"order": 1, "content": "x"}]}, headers=h)

    info = client.get(f"{API}/quizzes/{de['id']}/delete-info", headers=h)
    assert info.status_code == 200, info.text
    d = info.json()
    assert d["question_count"] == 1
    assert d["theory_count"] == 1


def test_xuat_de_ra_json(client, make_teacher):
    _, h = make_teacher()
    de = _tao_de(client, h)
    client.post(f"{API}/quizzes/{de['id']}/questions", json=_cau_hoi(), headers=h)

    r = client.get(f"{API}/quizzes/{de['id']}/export", headers=h)
    assert r.status_code == 200, r.text
    assert "2 + 2" in r.text


def test_khong_xuat_duoc_de_cua_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    de = _tao_de(client, hA)
    assert client.get(f"{API}/quizzes/{de['id']}/export", headers=hB).status_code in (403, 404)


# ─────────────────────────────────────────────────────────────
# Nhập câu từ ngân hàng
# ─────────────────────────────────────────────────────────────

def test_nhap_cau_tu_ngan_hang(client, make_teacher):
    _, h = make_teacher()

    client.post(f"{API}/questions/bulk", json={"questions": [
        {"question_text": "Ngân hàng: 5+5=?", "subject_code": "toan",
         "question_type": "TN", "difficulty": "NB", "grade": 6, "answer": "10"},
    ]}, headers=h)
    ids = [it["id"] for it in client.get(
        f"{API}/questions", params={"my_only": "true"}, headers=h).json()["items"]]
    assert ids

    de = _tao_de(client, h)
    r = client.post(f"{API}/quizzes/{de['id']}/import-questions",
                    json={"question_ids": ids[:1]}, headers=h)
    assert r.status_code == 200, r.text
    assert client.get(f"{API}/quizzes/{de['id']}/questions", headers=h).json()


def test_khong_nhap_duoc_cau_rieng_tu_cua_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()

    client.post(f"{API}/questions/bulk", json={"questions": [
        {"question_text": "Riêng tư của A trong quiz", "subject_code": "toan",
         "question_type": "TN", "difficulty": "NB", "grade": 6, "answer": "A"},
    ]}, headers=hA)
    ids_a = [it["id"] for it in client.get(
        f"{API}/questions", params={"my_only": "true"}, headers=hA).json()["items"]]

    de_b = _tao_de(client, hB)
    r = client.post(f"{API}/quizzes/{de_b['id']}/import-questions",
                    json={"question_ids": ids_a}, headers=hB)
    # Hoặc bị từ chối, hoặc chấp nhận nhưng KHÔNG nhập câu nào
    if r.status_code == 200:
        assert client.get(f"{API}/quizzes/{de_b['id']}/questions", headers=hB).json() == [], (
            "câu riêng tư của A bị nhập vào đề của B"
        )
    else:
        assert r.status_code in (403, 404)
