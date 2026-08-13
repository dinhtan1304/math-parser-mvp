"""
Test hành vi cho ngân hàng câu hỏi — 14 endpoint, trước đây chỉ ~5 test.

TẠI SAO VÙNG NÀY QUAN TRỌNG
Ngân hàng là tài sản dài hạn của giáo viên: mọi tính năng khác (ráp đề theo ma
trận, sinh câu tương tự, tạo quiz, xuất file) đều rút câu từ đây. Một lỗi lọc
làm câu của người này lọt sang người kia là hỏng niềm tin không cứu được.

Phần đã có nơi khác: cộng đồng + clone (test_community_bank.py), hiển thị công
khai (test_compliance.py), chặn vô danh (test_auth_contract.py).
"""

API = "/api/v1"


def _nap(client, headers, qs):
    r = client.post(f"{API}/questions/bulk", json={"questions": qs}, headers=headers)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _cau(text, **kw):
    q = {
        "question_text": text, "subject_code": "toan", "question_type": "TN",
        "difficulty": "NB", "grade": 6, "chapter": "Chương I. Tập hợp", "answer": "A",
    }
    q.update(kw)
    return q


def _ids(client, headers, **params):
    p = {"my_only": "true", "page_size": 100}
    p.update(params)
    return client.get(f"{API}/questions", params=p, headers=headers).json()


# ─────────────────────────────────────────────────────────────
# Nạp câu + chống trùng
# ─────────────────────────────────────────────────────────────

def test_nap_hang_loat_va_dem_dung(client, make_teacher):
    _, h = make_teacher()
    d = _nap(client, h, [_cau(f"Câu nạp {i}") for i in range(5)])
    assert d["count"] == 5, d
    assert _ids(client, h)["total"] == 5


def test_nap_lai_cau_giong_het_thi_bi_bo_qua(client, make_teacher):
    """Chống trùng bằng content_hash. Không có nó, tải lại cùng một đề sẽ nhân
    đôi ngân hàng."""
    _, h = make_teacher()
    _nap(client, h, [_cau("Câu trùng tuyệt đối")])
    d = _nap(client, h, [_cau("Câu trùng tuyệt đối")])

    assert d["skipped"] >= 1, f"nạp lại câu y hệt mà không bị bỏ qua: {d}"
    assert _ids(client, h)["total"] == 1


def test_khoang_trang_khac_nhau_van_tinh_la_trung(client, make_teacher):
    """content_hash chuẩn hóa khoảng trắng trước khi băm."""
    _, h = make_teacher()
    _nap(client, h, [_cau("Tính  tổng   hai số")])
    d = _nap(client, h, [_cau("Tính tổng hai số")])
    assert d["skipped"] >= 1, f"khác mỗi khoảng trắng mà lọt qua dedup: {d}"


def test_cau_rong_bi_tu_choi(client, make_teacher):
    _, h = make_teacher()
    d = _nap(client, h, [_cau("   ")])
    assert d["count"] == 0, "câu rỗng lọt vào ngân hàng"


# ─────────────────────────────────────────────────────────────
# Lọc & sắp xếp
# ─────────────────────────────────────────────────────────────

def test_loc_theo_muc_do(client, make_teacher):
    _, h = make_teacher()
    _nap(client, h, [
        _cau("Nhận biết 1", difficulty="NB"),
        _cau("Thông hiểu 1", difficulty="TH"),
        _cau("Vận dụng 1", difficulty="VD"),
    ])
    d = _ids(client, h, difficulty="TH")
    assert d["total"] == 1
    assert d["items"][0]["difficulty"] == "TH"


def test_loc_theo_lop_va_chuong(client, make_teacher):
    _, h = make_teacher()
    _nap(client, h, [
        _cau("Lớp 6 chương I", grade=6, chapter="Chương I. Tập hợp"),
        _cau("Lớp 7 chương II", grade=7, chapter="Chương II. Số hữu tỉ"),
    ])
    assert _ids(client, h, grade=7)["total"] == 1
    assert _ids(client, h, chapter="Chương I. Tập hợp")["total"] == 1


def test_loc_theo_dang_cau(client, make_teacher):
    _, h = make_teacher()
    _nap(client, h, [
        _cau("Trắc nghiệm", question_type="TN"),
        _cau("Tự luận", question_type="TL"),
    ])
    d = _ids(client, h, type="TL")
    assert d["total"] == 1 and d["items"][0]["question_type"] == "TL"


def test_tim_theo_tu_khoa(client, make_teacher):
    _, h = make_teacher()
    _nap(client, h, [_cau("Phương trình bậc hai một ẩn"), _cau("Hình học không gian")])
    d = _ids(client, h, keyword="bậc hai")
    assert d["total"] >= 1
    assert any("bậc hai" in it["question_text"] for it in d["items"])


def test_sap_xep_theo_ngay_tao(client, make_teacher):
    _, h = make_teacher()
    _nap(client, h, [_cau(f"Sắp xếp {i}") for i in range(3)])

    giam = _ids(client, h, sort_by="created_at", sort_order="desc")["items"]
    tang = _ids(client, h, sort_by="created_at", sort_order="asc")["items"]
    assert [q["id"] for q in giam] == list(reversed([q["id"] for q in tang]))


def test_phan_trang_khong_lap_cau(client, make_teacher):
    _, h = make_teacher()
    _nap(client, h, [_cau(f"Phân trang {i}") for i in range(7)])

    t1 = client.get(f"{API}/questions", params={"my_only": "true", "page": 1, "page_size": 3},
                    headers=h).json()
    t2 = client.get(f"{API}/questions", params={"my_only": "true", "page": 2, "page_size": 3},
                    headers=h).json()
    assert t1["total"] == 7
    assert len(t1["items"]) == 3 and len(t2["items"]) == 3
    assert not ({q["id"] for q in t1["items"]} & {q["id"] for q in t2["items"]}), "trang chồng nhau"


def test_danh_sach_bo_loc_phan_anh_du_lieu_that(client, make_teacher):
    _, h = make_teacher()
    _nap(client, h, [_cau("Cho bộ lọc", chapter="Chương X. Riêng biệt")])

    f = client.get(f"{API}/questions/filters", headers=h)
    assert f.status_code == 200, f.text
    assert "Chương X. Riêng biệt" in str(f.json())


# ─────────────────────────────────────────────────────────────
# Cách ly quyền sở hữu
# ─────────────────────────────────────────────────────────────

def test_khong_thay_cau_rieng_tu_cua_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    _nap(client, hA, [_cau("RIENG_TU_CUA_A")])

    assert _ids(client, hB)["total"] == 0
    assert "RIENG_TU_CUA_A" not in str(client.get(
        f"{API}/questions", params={"page_size": 100}, headers=hB).json())


def test_khong_sua_duoc_cau_cua_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    _nap(client, hA, [_cau("Câu của A không cho sửa")])
    qid = _ids(client, hA)["items"][0]["id"]

    r = client.put(f"{API}/questions/{qid}",
                   json={"question_text": "B chiếm quyền"}, headers=hB)
    assert r.status_code in (403, 404)
    assert client.get(f"{API}/questions/{qid}", headers=hA).json()["question_text"] != "B chiếm quyền"


def test_khong_xoa_duoc_cau_cua_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    _nap(client, hA, [_cau("Câu của A không cho xóa")])
    qid = _ids(client, hA)["items"][0]["id"]

    assert client.delete(f"{API}/questions/{qid}", headers=hB).status_code in (403, 404)
    assert client.get(f"{API}/questions/{qid}", headers=hA).status_code == 200


def test_xoa_hang_loat_chi_dung_toi_cau_cua_minh(client, make_teacher):
    """Nguy hiểm nhất: gửi id của người khác vào danh sách xóa hàng loạt."""
    _, hA = make_teacher()
    _, hB = make_teacher()
    _nap(client, hA, [_cau("Của A, B không được xóa")])
    id_a = _ids(client, hA)["items"][0]["id"]
    _nap(client, hB, [_cau("Của B")])
    id_b = _ids(client, hB)["items"][0]["id"]

    r = client.post(f"{API}/questions/bulk-delete",
                    json={"question_ids": [id_a, id_b]}, headers=hB)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 1, f"xóa cả câu của A: {r.json()}"
    assert client.get(f"{API}/questions/{id_a}", headers=hA).status_code == 200


# ─────────────────────────────────────────────────────────────
# Sửa / xóa của chính mình
# ─────────────────────────────────────────────────────────────

def test_sua_cau_cua_minh(client, make_teacher):
    _, h = make_teacher()
    _nap(client, h, [_cau("Trước khi sửa")])
    qid = _ids(client, h)["items"][0]["id"]

    r = client.put(f"{API}/questions/{qid}",
                   json={"question_text": "Sau khi sửa", "difficulty": "VD"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["question_text"] == "Sau khi sửa"
    assert r.json()["difficulty"] == "VD"


def test_xoa_cau_cua_minh(client, make_teacher):
    _, h = make_teacher()
    _nap(client, h, [_cau("Sẽ bị xóa")])
    qid = _ids(client, h)["items"][0]["id"]

    client.delete(f"{API}/questions/{qid}", headers=h)
    assert client.get(f"{API}/questions/{qid}", headers=h).status_code == 404


# ─────────────────────────────────────────────────────────────
# Phát hiện trùng lặp
# ─────────────────────────────────────────────────────────────

def test_tim_cau_trung_trong_ngan_hang(client, make_teacher):
    _, h = make_teacher()
    _nap(client, h, [_cau("Trùng lặp mẫu A"), _cau("Hoàn toàn khác biệt")])

    r = client.get(f"{API}/questions/duplicates", params={"threshold": 0.85}, headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "groups" in d and "total_groups" in d


def test_tim_trung_khong_lo_cau_cua_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    _nap(client, hA, [_cau("BIMAT_TRUNG_LAP_CUA_A")])

    d = client.get(f"{API}/questions/duplicates", headers=hB).json()
    assert "BIMAT_TRUNG_LAP_CUA_A" not in str(d)


# ─────────────────────────────────────────────────────────────
# Hiển thị công khai
# ─────────────────────────────────────────────────────────────

def test_cau_khong_co_dap_an_thi_khong_cho_cong_khai(client, make_teacher):
    """Chia sẻ câu thiếu đáp án lên cộng đồng là làm bẩn ngân hàng chung."""
    _, h = make_teacher()
    _nap(client, h, [_cau("Câu chưa có đáp án", answer=None)])
    qid = _ids(client, h)["items"][0]["id"]

    r = client.patch(f"{API}/questions/bulk-visibility",
                     json={"question_ids": [qid], "is_public": True}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["skipped_no_answer"] >= 1, f"câu thiếu đáp án vẫn được công khai: {r.json()}"


# ─────────────────────────────────────────────────────────────
# Báo lỗi câu hỏi
# ─────────────────────────────────────────────────────────────

def test_bao_loi_cau_hoi(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    _nap(client, hA, [_cau("Câu bị báo lỗi")])
    qid = _ids(client, hA)["items"][0]["id"]
    client.patch(f"{API}/questions/bulk-visibility",
                 json={"question_ids": [qid], "is_public": True}, headers=hA)

    r = client.post(f"{API}/questions/{qid}/report",
                    json={"reason": "wrong_answer", "detail": "Đáp án B mới đúng"}, headers=hB)
    assert r.status_code == 201, r.text


def test_bao_loi_hai_lan_cung_mot_cau_bi_chan(client, make_teacher):
    """Ràng buộc duy nhất (question_id, reporter_id) — chặn spam báo lỗi."""
    _, hA = make_teacher()
    _, hB = make_teacher()
    _nap(client, hA, [_cau("Câu bị báo hai lần")])
    qid = _ids(client, hA)["items"][0]["id"]
    client.patch(f"{API}/questions/bulk-visibility",
                 json={"question_ids": [qid], "is_public": True}, headers=hA)

    client.post(f"{API}/questions/{qid}/report", json={"reason": "duplicate"}, headers=hB)
    lan2 = client.post(f"{API}/questions/{qid}/report", json={"reason": "duplicate"}, headers=hB)
    assert lan2.status_code in (400, 409), f"báo lỗi trùng lọt qua (mã {lan2.status_code})"


def test_bao_loi_voi_ly_do_khong_hop_le(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    _nap(client, hA, [_cau("Câu test lý do lạ")])
    qid = _ids(client, hA)["items"][0]["id"]

    r = client.post(f"{API}/questions/{qid}/report",
                    json={"reason": "ly_do_bia_dat"}, headers=hB)
    assert r.status_code in (400, 422)
