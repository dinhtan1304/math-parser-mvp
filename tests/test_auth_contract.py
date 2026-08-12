"""
Hợp đồng xác thực cho TOÀN BỘ route — lưới an toàn diện rộng.

TẠI SAO FILE NÀY TỒN TẠI
Audit 2026-08-11 cho thấy ~83 endpoint (55%) không có test nào chạm tới. Loại
lỗi nguy hiểm nhất trong khoảng trống đó không phải logic sai, mà là **vô tình
gỡ mất `Depends(get_current_active_user)`** khi refactor — không có gì báo, và
dữ liệu của giáo viên này lộ sang giáo viên khác.

File này quét mọi route đã mount, gọi KHÔNG kèm token, và bắt buộc phải bị chặn
(401/403) TRỪ những đường nằm trong `PUBLIC_ROUTES` bên dưới.

`PUBLIC_ROUTES` chính là **hợp đồng bảo mật được viết ra**: thêm một route công
khai mới thì phải thêm vào đây kèm lý do — tức là một quyết định có ý thức, chứ
không phải tai nạn.
"""

import pytest

SKIP_METHODS = {"HEAD", "OPTIONS"}

# ─────────────────────────────────────────────────────────────
# Danh sách đường CÔNG KHAI CÓ CHỦ Ý. (method, path) → lý do.
# Thêm mục vào đây = tuyên bố "route này công khai là đúng ý".
# ─────────────────────────────────────────────────────────────
PUBLIC_ROUTES: dict[tuple[str, str], str] = {
    # ── Hạ tầng ──
    ("GET", "/health"): "healthcheck cho Docker/Railway/load balancer",
    ("GET", "/docs"): "Swagger UI",
    ("GET", "/redoc"): "ReDoc",
    ("GET", "/docs/oauth2-redirect"): "phụ trợ Swagger",
    ("GET", "/api/v1/openapi.json"): "OpenAPI schema",

    # ── Xác thực: bản chất phải công khai ──
    ("POST", "/api/v1/auth/login"): "đăng nhập",
    ("POST", "/api/v1/auth/register"): "đăng ký",
    ("POST", "/api/v1/auth/refresh"): "đổi refresh token (tự nó là chứng danh)",
    ("POST", "/api/v1/auth/forgot-password"): "quên mật khẩu",
    ("POST", "/api/v1/auth/reset-password"): "đặt lại mật khẩu bằng token trong email",

    # ── Quyền chủ thể dữ liệu ──
    ("POST", "/api/v1/me/delete-cancel"): (
        "hủy yêu cầu xóa tài khoản. PHẢI công khai: tài khoản đang chờ xóa "
        "KHÔNG đăng nhập được, đây là đường duy nhất để tự khôi phục"
    ),

    # ── Chia sẻ công khai ──
    ("GET", "/api/v1/quizzes/by-code/{code}"): "mở đề bằng mã chia sẻ",
    ("GET", "/api/v1/pages/public/{slug}"): "trang giáo viên đã xuất bản",
    ("GET", "/api/v1/subjects"): "danh mục môn học GDPT 2018 — dữ liệu tham chiếu",

    # ── Làm bài vô danh (luồng IELTS qua link) ──
    ("POST", "/api/v1/quiz-attempts/start"): "khách làm bài qua link, student_id=NULL",
    ("POST", "/api/v1/quiz-attempts/{attempt_id}/submit"): "khách nộp bài",
    ("GET", "/api/v1/quiz-attempts/{attempt_id}/hint/{question_id}"): "gợi ý cho khách",
    ("GET", "/api/v1/quiz-attempts/{attempt_id}/writing-grades"): (
        "khách xem điểm bài viết của chính mình. Xem cảnh báo ở "
        "test_luot_lam_vo_danh_ai_cung_doc_duoc"
    ),
    ("POST", "/api/v1/quiz-attempts/{attempt_id}/writing-grades/retry/{question_id}"): (
        "khách yêu cầu chấm lại"
    ),

    # ── Có cơ chế bảo vệ riêng, không dùng JWT ──
    ("GET", "/api/v1/parser/stream/{job_id}"): (
        "SSE dùng token dùng-một-lần trong query (JWT trong URL sẽ rò qua "
        "lịch sử trình duyệt / referrer / log máy chủ)"
    ),
    ("GET", "/api/v1/media/proxy"): (
        "proxy media chặn theo danh sách domain cho phép thay vì auth — "
        "xem test_proxy_chan_domain_ngoai_danh_sach"
    ),
    ("GET", "/api/v1/pages/check-slug/{slug}"): (
        "kiểm tra slug còn trống khi tạo trang. LƯU Ý: cho phép dò xem slug nào "
        "đã bị chiếm — rò rỉ nhẹ, chấp nhận được vì slug vốn công khai"
    ),
}


def _all_routes(app):
    """(method, path) của mọi route đã mount, trừ HEAD/OPTIONS."""
    out = []
    for r in app.routes:
        if not hasattr(r, "methods"):
            continue
        for m in sorted(set(r.methods) - SKIP_METHODS):
            out.append((m, r.path))
    return sorted(out)


def _concrete(path: str) -> str:
    """Thay {tham_so} bằng '1' để gọi thật được."""
    for part in path.split("/"):
        if part.startswith("{") and part.endswith("}"):
            path = path.replace(part, "1")
    return path


def _route_params():
    from app.main import app
    return _all_routes(app)


@pytest.mark.parametrize("method,path", _route_params())
def test_route_chan_truy_cap_vo_danh(client, method, path):
    """Mọi route không nằm trong PUBLIC_ROUTES phải chặn khi không có token."""
    if (method, path) in PUBLIC_ROUTES:
        pytest.skip(f"công khai có chủ ý: {PUBLIC_ROUTES[(method, path)]}")

    resp = client.request(method, _concrete(path))

    assert resp.status_code in (401, 403), (
        f"{method} {path} trả {resp.status_code} khi gọi KHÔNG có token.\n"
        f"Nếu đây là chủ ý → thêm vào PUBLIC_ROUTES kèm lý do.\n"
        f"Nếu KHÔNG → route này đang lộ dữ liệu; kiểm tra lại Depends(...).\n"
        f"Body: {resp.text[:300]}"
    )


def test_danh_sach_cong_khai_khong_co_muc_thua():
    """PUBLIC_ROUTES không được chứa route đã bị xóa khỏi app.

    Giữ danh sách khớp thực tế: một mục thừa nghĩa là hợp đồng bảo mật nói về
    thứ không còn tồn tại, và sẽ âm thầm cho qua nếu route đó quay lại.
    """
    from app.main import app

    thuc_te = set(_all_routes(app))
    thua = [r for r in PUBLIC_ROUTES if r not in thuc_te]
    assert not thua, f"PUBLIC_ROUTES có mục không còn tồn tại trong app: {thua}"


def test_so_route_khong_tut_bat_ngo():
    """Chốt số lượng route để việc mất/thêm hàng loạt không trôi qua lặng lẽ."""
    from app.main import app

    n = len(_all_routes(app))
    assert n >= 140, (
        f"Chỉ còn {n} route (trước đây 149). Có router nào không được mount?"
    )


# ─────────────────────────────────────────────────────────────
# Các đường công khai có cơ chế bảo vệ riêng — test cơ chế đó
# ─────────────────────────────────────────────────────────────

def test_proxy_chan_domain_ngoai_danh_sach():
    """/media/proxy không có auth, nên danh sách domain LÀ lớp bảo vệ duy nhất.

    Nếu lớp này hỏng, endpoint thành công cụ SSRF: ép máy chủ gọi tới địa chỉ
    nội bộ (169.254.169.254 metadata, localhost, mạng LAN).
    """
    from app.api.media import PROXY_ALLOWED_DOMAINS

    assert "drive.google.com" in PROXY_ALLOWED_DOMAINS
    # Không được lọt các đích nội bộ / tùy ý
    for xau in ("localhost", "127.0.0.1", "169.254.169.254", "evil.com", ""):
        assert xau not in PROXY_ALLOWED_DOMAINS


def test_proxy_tu_choi_domain_la(client):
    r = client.get("/api/v1/media/proxy", params={"url": "http://169.254.169.254/latest/meta-data/"})
    assert r.status_code == 400, f"proxy phải từ chối domain lạ, nhận {r.status_code}"


def test_proxy_tu_choi_dia_chi_noi_bo(client):
    r = client.get("/api/v1/media/proxy", params={"url": "http://localhost:8000/api/v1/questions"})
    assert r.status_code == 400


def test_luot_lam_vo_danh_ai_cung_doc_duoc(client):
    """GHI NHẬN hành vi hiện tại: lượt làm vô danh KHÔNG được bảo vệ.

    `_ensure_attempt_access` (app/api/ielts_writing.py) chỉ kiểm tra quyền khi
    `attempt.student_id IS NOT NULL`. Với lượt làm của khách (student_id=NULL),
    bất kỳ ai biết attempt_id đều đọc được điểm + nhận xét bài viết. Mà
    attempt_id là số nguyên TUẦN TỰ nên dò được bằng cách đếm lên.

    Giảm nhẹ: lượt làm vô danh KHÔNG gắn danh tính nào (không tên, không email
    — xem tests/test_student_data_minimal.py), nên rò rỉ là nội dung bài viết +
    điểm, không kèm người viết.

    Test này khóa hành vi hiện tại. Nếu sau này thêm cơ chế bảo vệ (token theo
    lượt làm chẳng hạn), test sẽ đỏ và nhắc cập nhật cả PUBLIC_ROUTES.
    """
    r = client.get("/api/v1/quiz-attempts/999999/writing-grades")
    # 404 vì không tồn tại — điều quan trọng là KHÔNG phải 401:
    # tức là máy chủ đã tra DB trước khi nghĩ tới chuyện xác thực.
    assert r.status_code == 404


def test_lam_bai_cua_nguoi_dung_da_dang_nhap_thi_bi_chan(client, make_teacher):
    """Ngược lại: lượt làm CÓ chủ thì người khác không đọc được."""
    _, hA = make_teacher()
    _, hB = make_teacher()

    quiz = client.post("/api/v1/quizzes", json={"name": "Đề thử quyền"}, headers=hA).json()
    client.patch(f"/api/v1/quizzes/{quiz['id']}", json={"status": "published"}, headers=hA)

    att = client.post("/api/v1/quiz-attempts/start", json={"quiz_id": quiz["id"]}, headers=hA)
    assert att.status_code == 201, att.text
    attempt_id = att.json()["id"]

    # Giáo viên B không xem được lượt làm của A
    r = client.get(f"/api/v1/quiz-attempts/{attempt_id}/writing-grades", headers=hB)
    assert r.status_code == 403

    # Khách vô danh cũng không
    r2 = client.get(f"/api/v1/quiz-attempts/{attempt_id}/writing-grades")
    assert r2.status_code == 403
