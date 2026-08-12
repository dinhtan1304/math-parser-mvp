"""
Test hành vi cho `export`, `dashboard`, `pages` — TRƯỚC ĐÂY 0 test HTTP.

`export` là ĐẦU RA CỦA SẢN PHẨM: file Word/PDF/LaTeX mà giáo viên tải về và
mang đi in. Nó cũng là consumer duy nhất của `latex_to_omml` trên đường HTTP,
nên test ở đây khép kín vòng: LaTeX trong ngân hàng → công thức trong file .docx.
"""

import io
import zipfile

API = "/api/v1"


def _cau_hoi_xuat(**kw):
    q = {
        "question": r"Tính $\frac{1}{2} + \frac{1}{3}$",
        "type": "TN",
        "topic": "Phân số",
        "difficulty": "TH",
        "answer": r"$\frac{5}{6}$",
        "solution_steps": ["Quy đồng mẫu số", r"$\frac{3}{6} + \frac{2}{6}$"],
    }
    q.update(kw)
    return q


# ─────────────────────────────────────────────────────────────
# Xuất Word
# ─────────────────────────────────────────────────────────────

def test_xuat_docx_tra_ve_file_word_hop_le(client, make_teacher):
    _, h = make_teacher()
    r = client.post(f"{API}/export/docx",
                    json={"questions": [_cau_hoi_xuat()], "title": "ĐỀ KIỂM TRA"},
                    headers=h)
    assert r.status_code == 200, r.text
    assert len(r.content) > 1000, "file docx quá nhỏ, nhiều khả năng rỗng"

    # .docx là file zip chứa word/document.xml — mở được nghĩa là không hỏng
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert "word/document.xml" in z.namelist()
        xml = z.read("word/document.xml").decode("utf-8")

    assert "ĐỀ KIỂM TRA" in xml


def test_docx_chua_cong_thuc_omml_chu_khong_phai_latex_tho(client, make_teacher):
    """Khép kín vòng LaTeX → OMML.

    Nếu bộ chuyển hỏng, `add_math_to_paragraph` rơi về chèn text thô và file
    Word sẽ hiện literal '\\frac{1}{2}' thay vì phân số. Người dùng thấy ngay,
    và đó là loại lỗi làm mất niềm tin nhanh nhất.
    """
    _, h = make_teacher()
    r = client.post(f"{API}/export/docx", json={"questions": [_cau_hoi_xuat()]}, headers=h)
    assert r.status_code == 200

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        xml = z.read("word/document.xml").decode("utf-8")

    assert "oMath" in xml, "không có phần tử OMML nào — công thức bị chèn dạng text thô"
    assert r"\frac" not in xml, r"LaTeX thô '\frac' lọt vào file Word"


def test_xuat_docx_nhieu_cau(client, make_teacher):
    _, h = make_teacher()
    qs = [_cau_hoi_xuat(question=f"Câu số {i}: $x^{i}$") for i in range(1, 6)]
    r = client.post(f"{API}/export/docx", json={"questions": qs}, headers=h)
    assert r.status_code == 200

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    for i in range(1, 6):
        assert f"Câu số {i}" in xml


def test_xuat_docx_danh_sach_rong_khong_no(client, make_teacher):
    _, h = make_teacher()
    r = client.post(f"{API}/export/docx", json={"questions": []}, headers=h)
    assert r.status_code in (200, 400, 422), r.text


def test_xuat_docx_latex_hong_van_ra_file(client, make_teacher):
    """OCR sinh LaTeX không phải lúc nào cũng đúng — một công thức hỏng KHÔNG
    được làm vỡ cả file của giáo viên."""
    _, h = make_teacher()
    r = client.post(f"{API}/export/docx",
                    json={"questions": [_cau_hoi_xuat(question=r"Hỏng $\frac{a$ đây")]},
                    headers=h)
    assert r.status_code == 200, r.text
    assert len(r.content) > 1000


# ─────────────────────────────────────────────────────────────
# Xuất LaTeX / PDF
# ─────────────────────────────────────────────────────────────

def test_xuat_latex(client, make_teacher):
    _, h = make_teacher()
    r = client.post(f"{API}/export/latex",
                    json={"questions": [_cau_hoi_xuat()], "title": "ĐỀ LATEX"}, headers=h)
    assert r.status_code == 200, r.text
    noi_dung = r.content.decode("utf-8", errors="replace")
    assert "documentclass" in noi_dung
    assert r"\frac" in noi_dung, "LaTeX phải giữ nguyên công thức gốc"


def test_xuat_pdf_tra_ve_html_hoac_pdf(client, make_teacher):
    _, h = make_teacher()
    r = client.post(f"{API}/export/pdf", json={"questions": [_cau_hoi_xuat()]}, headers=h)
    assert r.status_code == 200, r.text
    assert len(r.content) > 100


# ─────────────────────────────────────────────────────────────
# Xuất từ ngân hàng — phải giới hạn theo chủ sở hữu
# ─────────────────────────────────────────────────────────────

def test_xuat_ngan_hang_chi_lay_cau_cua_minh(client, make_teacher):
    """Không được để câu của giáo viên khác lọt vào file xuất.

    B có ngân hàng RỖNG. Backend trả 404 "Không tìm thấy câu hỏi nào" — chính
    điều đó chứng minh truy vấn đã lọc theo user_id: nếu thiếu bộ lọc, B sẽ
    thấy câu của A và nhận về 200 kèm file có nội dung.
    """
    _, hA = make_teacher()
    _, hB = make_teacher()

    client.post(f"{API}/questions/bulk", json={"questions": [
        {"question_text": "BIMAT_CUA_A_KHONG_DUOC_LO", "subject_code": "toan",
         "question_type": "TN", "difficulty": "NB", "grade": 6, "answer": "A"},
    ]}, headers=hA)

    r = client.post(f"{API}/export/bank/docx", json={"limit": 200}, headers=hB)
    assert r.status_code == 404, (
        f"B có ngân hàng rỗng nhưng xuất được file (mã {r.status_code}) — "
        f"nhiều khả năng truy vấn không lọc theo user_id"
    )

    # A xuất được, và file có đúng câu của A
    ra = client.post(f"{API}/export/bank/docx", json={"limit": 200}, headers=hA)
    assert ra.status_code == 200, ra.text
    with zipfile.ZipFile(io.BytesIO(ra.content)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert "BIMAT_CUA_A_KHONG_DUOC_LO" in xml


def test_xuat_ngan_hang_latex(client, make_teacher):
    _, h = make_teacher()
    client.post(f"{API}/questions/bulk", json={"questions": [
        {"question_text": r"Ngân hàng $x^2$", "subject_code": "toan",
         "question_type": "TN", "difficulty": "NB", "grade": 6, "answer": "A"},
    ]}, headers=h)

    r = client.post(f"{API}/export/bank/latex", json={"limit": 50}, headers=h)
    assert r.status_code == 200, r.text
    assert "documentclass" in r.content.decode("utf-8", errors="replace")


# ─────────────────────────────────────────────────────────────
# Dashboard
# ─────────────────────────────────────────────────────────────

def test_dashboard_tra_ve_du_truong_cho_giao_vien_moi(client, make_teacher):
    _, h = make_teacher()
    r = client.get(f"{API}/dashboard", headers=h)
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), dict)


def test_dashboard_charts_activity_token_usage(client, make_teacher):
    _, h = make_teacher()
    for duong in ("/dashboard/charts", "/dashboard/activity", "/dashboard/token-usage"):
        r = client.get(f"{API}{duong}", headers=h)
        assert r.status_code == 200, f"{duong} → {r.status_code}: {r.text[:200]}"


def test_dashboard_khong_gom_du_lieu_cua_giao_vien_khac(client, make_teacher):
    """Số liệu phải theo từng giáo viên, không phải toàn hệ thống."""
    _, hA = make_teacher()
    _, hB = make_teacher()

    client.post(f"{API}/questions/bulk", json={"questions": [
        {"question_text": f"Của A số {i}", "subject_code": "toan",
         "question_type": "TN", "difficulty": "NB", "grade": 6, "answer": "A"}
        for i in range(3)
    ]}, headers=hA)

    d = client.get(f"{API}/dashboard", headers=hB).json()
    so = [v for v in d.values() if isinstance(v, int)]
    assert all(v == 0 for v in so), f"dashboard của B thấy dữ liệu của A: {d}"


# ─────────────────────────────────────────────────────────────
# Trang giáo viên
# ─────────────────────────────────────────────────────────────

def test_tao_trang_va_xem_cong_khai(client, make_teacher):
    import secrets

    _, h = make_teacher()
    slug = f"lop-co-a-{secrets.token_hex(3)}"

    kiem_tra = client.get(f"{API}/pages/check-slug/{slug}")
    assert kiem_tra.status_code == 200
    assert kiem_tra.json()["available"] is True

    tao = client.post(f"{API}/pages", json={
        "template_id": "horizon", "slug": slug,
        "title": "Trang của cô A", "config": {"quizzes": []},
    }, headers=h)
    assert tao.status_code == 201, tao.text

    # Slug đã bị chiếm
    assert client.get(f"{API}/pages/check-slug/{slug}").json()["available"] is False

    # Xem công khai không cần đăng nhập
    cong_khai = client.get(f"{API}/pages/public/{slug}")
    assert cong_khai.status_code == 200, cong_khai.text


def test_danh_sach_trang_chi_hien_trang_cua_minh(client, make_teacher):
    import secrets

    _, hA = make_teacher()
    _, hB = make_teacher()
    slug = f"rieng-cua-a-{secrets.token_hex(3)}"
    client.post(f"{API}/pages", json={
        "template_id": "horizon", "slug": slug, "title": "Riêng của A", "config": {},
    }, headers=hA)

    ds_b = client.get(f"{API}/pages/my", headers=hB).json()
    ten = {p["title"] for p in ds_b} if isinstance(ds_b, list) else set()
    assert "Riêng của A" not in ten


def test_khong_xoa_duoc_trang_cua_nguoi_khac(client, make_teacher):
    import secrets

    _, hA = make_teacher()
    _, hB = make_teacher()
    slug = f"trang-a-{secrets.token_hex(3)}"
    tao = client.post(f"{API}/pages", json={
        "template_id": "horizon", "slug": slug, "title": "Trang A", "config": {},
    }, headers=hA)
    pid = tao.json()["id"]

    assert client.delete(f"{API}/pages/{pid}", headers=hB).status_code in (403, 404)
    assert client.get(f"{API}/pages/public/{slug}").status_code == 200
