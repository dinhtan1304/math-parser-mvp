"""
Test cho trang giáo viên (`pages`) và tải media (`media`).

`pages` cho phép giáo viên dựng một trang CÔNG KHAI theo 10 mẫu giao diện, gắn
các đề quiz để học sinh vào làm. Vì trang này ai cũng xem được, hai bất biến
đáng test nhất là: slug phải duy nhất, và chỉ chủ trang mới sửa/xóa được.

`media` nhận ảnh và audio cho câu hỏi. Không có auth ở đường proxy nên chốt
chặn duy nhất là danh sách phần mở rộng và giới hạn dung lượng.

Phần proxy chống SSRF đã test ở tests/test_auth_contract.py.
"""

import secrets

API = "/api/v1"


def _slug():
    return f"lop-co-{secrets.token_hex(4)}"


def _tao_trang(client, headers, slug=None, **kw):
    payload = {
        "template_id": "horizon",
        "slug": slug or _slug(),
        "title": "Trang của cô Lan",
        "config": {"quizzes": [], "intro": "Chào các em"},
    }
    payload.update(kw)
    r = client.post(f"{API}/pages", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# ─────────────────────────────────────────────────────────────
# Vòng đời trang
# ─────────────────────────────────────────────────────────────

def test_tao_sua_xoa_trang(client, make_teacher):
    _, h = make_teacher()
    slug = _slug()
    trang = _tao_trang(client, h, slug=slug)

    assert trang["slug"] == slug
    assert trang["title"] == "Trang của cô Lan"

    sua = client.patch(f"{API}/pages/{trang['id']}",
                       json={"title": "Tên mới"}, headers=h)
    assert sua.status_code == 200, sua.text
    assert sua.json()["title"] == "Tên mới"

    assert client.delete(f"{API}/pages/{trang['id']}", headers=h).status_code == 204
    assert client.get(f"{API}/pages/public/{slug}").status_code == 404


def test_danh_sach_trang_cua_minh(client, make_teacher):
    _, h = make_teacher()
    trang = _tao_trang(client, h)

    ds = client.get(f"{API}/pages/my", headers=h)
    assert ds.status_code == 200, ds.text
    assert trang["id"] in {p["id"] for p in ds.json()}


def test_slug_phai_duy_nhat(client, make_teacher):
    """Trùng slug là hai giáo viên tranh nhau một địa chỉ công khai."""
    _, hA = make_teacher()
    _, hB = make_teacher()
    slug = _slug()
    _tao_trang(client, hA, slug=slug)

    r = client.post(f"{API}/pages", json={
        "template_id": "galaxy", "slug": slug, "title": "B chiếm slug", "config": {},
    }, headers=hB)
    assert r.status_code in (400, 409), f"hai trang cùng slug (mã {r.status_code})"


def test_kiem_tra_slug_con_trong(client, make_teacher):
    _, h = make_teacher()
    slug = _slug()

    assert client.get(f"{API}/pages/check-slug/{slug}").json()["available"] is True
    _tao_trang(client, h, slug=slug)
    assert client.get(f"{API}/pages/check-slug/{slug}").json()["available"] is False


def test_tu_choi_mau_giao_dien_khong_ton_tai(client, make_teacher):
    _, h = make_teacher()
    r = client.post(f"{API}/pages", json={
        "template_id": "mau_bia_dat", "slug": _slug(), "title": "X", "config": {},
    }, headers=h)
    assert r.status_code == 422


def test_moi_mau_giao_dien_deu_tao_duoc(client, make_teacher):
    """10 mẫu đều phải dùng được — FE có nút chọn cho cả 10."""
    from app.api.pages import VALID_TEMPLATE_IDS

    _, h = make_teacher()
    for mau in sorted(VALID_TEMPLATE_IDS):
        r = client.post(f"{API}/pages", json={
            "template_id": mau, "slug": _slug(), "title": f"Trang {mau}", "config": {},
        }, headers=h)
        assert r.status_code == 201, f"mẫu {mau} không tạo được: {r.text[:200]}"


# ─────────────────────────────────────────────────────────────
# Cách ly quyền sở hữu
# ─────────────────────────────────────────────────────────────

def test_khong_sua_duoc_trang_cua_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    trang = _tao_trang(client, hA, title="Trang gốc của A")

    r = client.patch(f"{API}/pages/{trang['id']}", json={"title": "B sửa"}, headers=hB)
    assert r.status_code in (403, 404)

    ds = client.get(f"{API}/pages/my", headers=hA).json()
    assert next(p for p in ds if p["id"] == trang["id"])["title"] == "Trang gốc của A"


def test_khong_xoa_duoc_trang_cua_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    slug = _slug()
    trang = _tao_trang(client, hA, slug=slug)

    assert client.delete(f"{API}/pages/{trang['id']}", headers=hB).status_code in (403, 404)
    assert client.get(f"{API}/pages/public/{slug}").status_code == 200


def test_danh_sach_trang_khong_lan_sang_nguoi_khac(client, make_teacher):
    _, hA = make_teacher()
    _, hB = make_teacher()
    _tao_trang(client, hA, title="RIENG_CUA_A")

    ds = client.get(f"{API}/pages/my", headers=hB).json()
    assert "RIENG_CUA_A" not in {p["title"] for p in ds}


# ─────────────────────────────────────────────────────────────
# Trang công khai
# ─────────────────────────────────────────────────────────────

def test_xem_trang_cong_khai_khong_can_dang_nhap(client, make_teacher):
    _, h = make_teacher()
    slug = _slug()
    _tao_trang(client, h, slug=slug, title="Trang mở cho học sinh")

    r = client.get(f"{API}/pages/public/{slug}")
    assert r.status_code == 200, r.text
    assert "Trang mở cho học sinh" in str(r.json())


def test_slug_khong_ton_tai_tra_404(client):
    assert client.get(f"{API}/pages/public/khong-he-ton-tai-{secrets.token_hex(3)}").status_code == 404


# ─────────────────────────────────────────────────────────────
# Tải media
# ─────────────────────────────────────────────────────────────

def test_tai_anh_len_duoc(client, make_teacher):
    _, h = make_teacher()
    # PNG 1x1 tối giản
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000a49444154789c63000100000500010d0a2db40000"
        "000049454e44ae426082"
    )
    r = client.post(f"{API}/media/upload",
                    files={"file": ("hinh.png", png, "image/png")}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["type"] == "image"
    assert r.json()["url"]


def test_tu_choi_phan_mo_rong_khong_cho_phep(client, make_teacher):
    """Chốt chặn quan trọng: không cho tải lên file thực thi."""
    _, h = make_teacher()
    for ten, noi_dung in [
        ("virus.exe", b"MZ\x90\x00"),
        ("script.js", b"alert(1)"),
        ("shell.php", b"<?php system($_GET[0]); ?>"),
        ("trang.html", b"<script>alert(1)</script>"),
    ]:
        r = client.post(f"{API}/media/upload",
                        files={"file": (ten, noi_dung, "application/octet-stream")}, headers=h)
        assert r.status_code == 400, f"{ten} lọt qua bộ lọc (mã {r.status_code})"


def test_tu_choi_file_qua_lon(client, make_teacher):
    _, h = make_teacher()
    from app.api.media import MAX_FILE_SIZE

    qua_lon = b"\x00" * (MAX_FILE_SIZE + 1024)
    r = client.post(f"{API}/media/upload",
                    files={"file": ("to.png", qua_lon, "image/png")}, headers=h)
    assert r.status_code == 400, f"file {MAX_FILE_SIZE // 1024 // 1024}MB+ lọt qua"


def test_danh_sach_duoi_file_cho_phep_khong_chua_thu_nguy_hiem(client):
    """Canh gác: thêm đuôi mới vào danh sách cũng không được mở đường thực thi.

    `.svg` nằm trong danh sách này cho tới 13/08/2026 và là lỗ hổng stored XSS
    thật: file tải lên được phục vụ bằng StaticFiles trên chính origin của
    backend, mà CSP cho phép `script-src 'self' 'unsafe-inline'` — mở trực tiếp
    một .svg chứa <script> là script chạy. Test này tìm ra nó.
    """
    from app.api.media import ALLOWED_EXTENSIONS

    nguy_hiem = {".exe", ".js", ".php", ".html", ".htm", ".svg", ".sh", ".bat", ".py",
                 ".xhtml", ".xml", ".swf"}
    lot = nguy_hiem & {e.lower() for e in ALLOWED_EXTENSIONS}
    assert not lot, f"đuôi nguy hiểm nằm trong danh sách cho phép: {lot}"


def test_tu_choi_tai_len_svg(client, make_teacher):
    """Chốt lại việc gỡ .svg ở tầng HTTP, không chỉ ở hằng số."""
    _, h = make_teacher()
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    r = client.post(f"{API}/media/upload",
                    files={"file": ("xss.svg", svg, "image/svg+xml")}, headers=h)
    assert r.status_code == 400, f"SVG lọt qua bộ lọc (mã {r.status_code})"
