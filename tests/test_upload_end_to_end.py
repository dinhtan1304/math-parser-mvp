"""
Test ĐẦU–CUỐI cho luồng tải lên: upload → OCR → duyệt markdown → parse → ngân hàng.

TẠI SAO FILE NÀY TỒN TẠI
Audit 2026-08-11 chỉ ra khoảng trống lớn nhất của cả hệ thống: `process_file`
(hàm lõi trong app/api/parser.py, 2451 dòng, 18 phụ thuộc) có 251 test phủ từng
MẢNH riêng lẻ nhưng KHÔNG bài nào chạy trọn vẹn từ đầu tới cuối. Sáu test tích
hợp cũ trong test_parser_pipeline.py đã bị tắt từ lâu vì mô tả luồng cũ
(needs_review + lưu ngân hàng trì hoãn) không còn đúng.

CÁCH TIẾP CẬN — khác hẳn bộ test cũ
Bộ cũ mock cả DB bằng MagicMock và patch hàng loạt hàm nội bộ (_is_text_poor_quality,
step3_classify, _parser_for_speed…). Hệ quả: chúng vỡ ngay khi cấu trúc bên trong
đổi, dù hành vi vẫn đúng — và đó chính là lý do chúng bị tắt.

Ở đây chỉ giả lập các BIÊN NGOÀI cần GPU/mạng:
    extract_local_ocr_artifact   (OCR — cần GPU)
    AIQuestionParser.parse       (Gemini — cần API key)
    index_document_for_rag       (Docling + embedding — cần GPU)
    capture_pdf_layout           (render trang bằng Marker/Surya — cần GPU)
Mọi thứ còn lại chạy THẬT: HTTP, background task, DB SQLite, dedup theo
content_hash, khớp chương trình, chuyển trạng thái, tạo bản nháp, ghi ngân hàng.

Nhờ vậy test bám vào HÀNH VI (trạng thái exam + nội dung ngân hàng) chứ không
bám vào cấu trúc bên trong, nên sống sót qua refactor.

Chạy được nhờ `process_file` lên lịch bằng `BackgroundTasks` — Starlette
TestClient chạy chúng đồng bộ ngay sau response, nên khi request trả về là
pipeline đã chạy xong.
"""

import secrets
from unittest.mock import AsyncMock, patch

import pytest

API = "/api/v1"


# ─────────────────────────────────────────────────────────────
# Dữ liệu giả lập cho hai biên ngoài
# ─────────────────────────────────────────────────────────────

MARKDOWN_DE = """# ĐỀ KIỂM TRA GIỮA KỲ I — TOÁN 6

Câu 1. Tính giá trị của $2^5$.

Câu 2. Tìm ước chung lớn nhất của 12 và 18.

Câu 3. Cho tập hợp $A = \\{1; 2; 3\\}$. Viết các tập con của A.
"""


def _artifact_ocr(text=MARKDOWN_DE, **kw):
    """Kết quả OCR giả lập — đúng hình dạng extract_local_ocr_artifact trả về.

    file_hash mặc định là NGẪU NHIÊN mỗi lần gọi, và điều đó là bắt buộc:
    process_file có tầng cache Phase 3b tra kết quả Gemini cũ theo file_hash
    (Exam.file_hash + status='completed'). Dùng hash cố định thì test sau ăn
    cache của test trước và hàm parse giả lập KHÔNG được gọi — chính lỗi này
    làm 3 test đỏ ở lần chạy đầu, dù chạy riêng lẻ thì xanh.
    """
    d = {
        "text": text,
        "file_hash": kw.pop("file_hash", f"hash_{secrets.token_hex(8)}"),
        "page_count": 1,
        "method": "paddle-vl",
        "image_map": {},
        "blocks": [],
        "figures": [],
        "warnings": [],
        "quality": {"score": 0.95},
        "latex": {"formula_count": 2},
    }
    d.update(kw)
    return d


def _cau_hoi_gemini():
    """Kết quả parse giả lập — đúng hình dạng AIQuestionParser.parse trả về."""
    return [
        {
            "question": "Tính giá trị của $2^5$.",
            "type": "TN", "topic": "Số học", "difficulty": "NB",
            "grade": 6, "chapter": "Chương I. Tập hợp các số tự nhiên",
            "lesson_title": "Lũy thừa với số mũ tự nhiên",
            "answer": "32", "solution_steps": ["$2^5 = 32$"], "cau_num": 1,
        },
        {
            "question": "Tìm ước chung lớn nhất của 12 và 18.",
            "type": "TN", "topic": "Số học", "difficulty": "TH",
            "grade": 6, "chapter": "Chương II. Tính chia hết",
            "lesson_title": "Ước chung. Ước chung lớn nhất",
            "answer": "6", "solution_steps": ["ƯCLN(12, 18) = 6"], "cau_num": 2,
        },
        {
            "question": "Cho tập hợp $A = \\{1; 2; 3\\}$. Viết các tập con của A.",
            "type": "TL", "topic": "Tập hợp", "difficulty": "VD",
            "grade": 6, "chapter": "Chương I. Tập hợp các số tự nhiên",
            "lesson_title": "Tập hợp",
            "answer": "8 tập con", "solution_steps": ["Số tập con = $2^3 = 8$"], "cau_num": 3,
        },
    ]


def _pdf_gia():
    """PDF tối giản nhưng hợp lệ để qua được bước kiểm tra định dạng."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )


class _BienNgoai:
    """Gộp hai patch của hai biên ngoài vào một context manager."""

    def __init__(self, *, ocr=None, cau_hoi=None, ocr_loi=None, parse_loi=None):
        self._ocr = ocr if ocr is not None else _artifact_ocr()
        self._cau = cau_hoi if cau_hoi is not None else _cau_hoi_gemini()
        self._ocr_loi = ocr_loi
        self._parse_loi = parse_loi
        self._patches = []

    def __enter__(self):
        ocr_mock = AsyncMock(
            side_effect=self._ocr_loi) if self._ocr_loi else AsyncMock(return_value=self._ocr)
        parse_mock = AsyncMock(
            side_effect=self._parse_loi) if self._parse_loi else AsyncMock(return_value=self._cau)

        self._patches = [
            patch("app.services.local_ocr_service.extract_local_ocr_artifact", ocr_mock),
            patch("app.services.ai_parser.AIQuestionParser.parse", parse_mock),
            # Hai biên nặng còn lại: cả hai nạp mô hình Surya/Docling thật nếu
            # không chặn — test chạy ~100s và phụ thuộc GPU. Chúng nằm ngoài
            # phạm vi quan tâm ở đây (index RAG và render ảnh trang), và đường
            # gọi chúng đã bọc try/except nên lỗi không ảnh hưởng kết quả parse.
            patch("app.services.hybrid_ingest.index_document_for_rag",
                  AsyncMock(return_value={"chunks": 0, "status": "skipped"})),
            patch("app.services.layout_assets.capture_pdf_layout",
                  AsyncMock(return_value={"pages": [], "figures": []})),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


def _tai_len(client, headers, ten="de-toan-6.pdf", **params):
    p = {"subject_hint": "toan", "grade": 6}
    p.update(params)
    return client.post(
        f"{API}/parser/parse",
        params=p,
        files={"file": (ten, _pdf_gia(), "application/pdf")},
        headers=headers,
    )


@pytest.fixture
def khong_review_markdown(monkeypatch):
    """Tắt bước duyệt markdown → pipeline chạy thẳng tới ngân hàng.

    Mặc định OCR_REVIEW_STEP=1 nên upload dừng ở trạng thái `ocr_review` chờ
    người sửa. Fixture này mô phỏng cấu hình chạy thẳng (OCR_REVIEW_STEP=0).
    """
    import app.api.parser as P
    monkeypatch.setattr(P, "OCR_REVIEW_STEP", False)


# ═════════════════════════════════════════════════════════════
# Luồng đầy đủ, chạy thẳng (không có bước duyệt markdown)
# ═════════════════════════════════════════════════════════════

def test_tai_len_den_ngan_hang_tron_ven(client, make_teacher, khong_review_markdown):
    """ĐÂY LÀ BÀI TEST QUAN TRỌNG NHẤT: tải lên → OCR → parse → ngân hàng."""
    _, h = make_teacher()

    with _BienNgoai():
        r = _tai_len(client, h)
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]

    # Trạng thái cuối
    tt = client.get(f"{API}/parser/status/{job_id}", headers=h)
    assert tt.status_code == 200, tt.text
    d = tt.json()
    assert d["status"] == "completed", f"pipeline không chạy tới đích: {d}"

    # Ba câu đã vào ngân hàng
    nh = client.get(f"{API}/questions", params={"my_only": "true", "page_size": 100},
                    headers=h).json()
    assert nh["total"] == 3, f"kỳ vọng 3 câu vào ngân hàng, thực tế {nh['total']}"

    noi_dung = {q["question_text"] for q in nh["items"]}
    assert any("2^5" in t for t in noi_dung), "câu 1 không vào ngân hàng"
    assert any("ước chung lớn nhất" in t.lower() for t in noi_dung), "câu 2 không vào"


def test_cau_vao_ngan_hang_giu_du_phan_loai(client, make_teacher, khong_review_markdown):
    """Metadata phân loại là thứ làm cho ngân hàng dùng được — mất nó thì ráp đề
    theo ma trận không lọc được gì."""
    _, h = make_teacher()

    with _BienNgoai():
        _tai_len(client, h)

    items = client.get(f"{API}/questions", params={"my_only": "true", "page_size": 100},
                       headers=h).json()["items"]
    cau1 = next(q for q in items if "2^5" in q["question_text"])

    assert cau1["difficulty"] == "NB"
    assert cau1["grade"] == 6
    assert cau1["answer"] == "32"
    assert cau1["question_type"] == "TN"
    assert cau1["chapter"], "mất thông tin chương"


def test_cau_tu_OCR_mang_nhan_nguon_goc(client, make_teacher, khong_review_markdown):
    """Điều 44 Luật Công nghiệp Công nghệ số — nội dung do máy tạo phải có nhãn."""
    _, h = make_teacher()

    with _BienNgoai():
        _tai_len(client, h)

    q = client.get(f"{API}/questions", params={"my_only": "true"}, headers=h).json()["items"][0]
    assert q.get("origin") in ("OCR_IMPORT", "AI_ASSISTED"), f"thiếu nhãn nguồn gốc: {q.get('origin')}"


def test_cau_gan_dung_ve_de_nguon(client, make_teacher, khong_review_markdown):
    """Truy vết được câu hỏi về đề gốc — cần cho lọc 'theo đề' ở trang ngân hàng."""
    _, h = make_teacher()

    with _BienNgoai():
        r = _tai_len(client, h)
    job_id = r.json()["job_id"]

    items = client.get(f"{API}/questions", params={"my_only": "true", "exam_id": job_id},
                       headers=h).json()
    assert items["total"] == 3, "câu không gắn về đúng đề nguồn"


def test_de_hien_trong_lich_su_kem_so_cau(client, make_teacher, khong_review_markdown):
    _, h = make_teacher()

    with _BienNgoai():
        r = _tai_len(client, h)
    job_id = r.json()["job_id"]

    ls = client.get(f"{API}/parser/history", headers=h).json()
    de = next(e for e in ls["items"] if e["id"] == job_id)
    assert de["status"] == "completed"
    assert de.get("question_count") == 3, f"đếm sai số câu trong lịch sử: {de}"


# ═════════════════════════════════════════════════════════════
# Bước duyệt markdown (cấu hình mặc định)
# ═════════════════════════════════════════════════════════════

def test_mac_dinh_dung_lai_o_buoc_duyet_markdown(client, make_teacher):
    """OCR_REVIEW_STEP=1 (mặc định): OCR xong thì DỪNG cho người xem/sửa markdown,
    chưa parse, chưa có câu nào vào ngân hàng."""
    _, h = make_teacher()

    with _BienNgoai():
        r = _tai_len(client, h)
    job_id = r.json()["job_id"]

    d = client.get(f"{API}/parser/status/{job_id}", headers=h).json()
    assert d["status"] == "ocr_review", f"không dừng ở bước duyệt markdown: {d['status']}"

    assert client.get(f"{API}/questions", params={"my_only": "true"},
                      headers=h).json()["total"] == 0, "đã ghi ngân hàng khi chưa qua bước duyệt"


def test_doc_duoc_markdown_de_sua(client, make_teacher):
    _, h = make_teacher()

    with _BienNgoai():
        r = _tai_len(client, h)
    job_id = r.json()["job_id"]

    ocr = client.get(f"{API}/parser/{job_id}/ocr", headers=h)
    assert ocr.status_code == 200, ocr.text
    assert "Câu 1" in ocr.json()["markdown"]


def test_sua_markdown_roi_parse_thi_dung_ban_da_sua(client, make_teacher):
    """Toàn bộ giá trị của bước duyệt nằm ở đây: bản vào ngân hàng phải là bản
    người dùng đã sửa, KHÔNG phải bản OCR thô."""
    _, h = make_teacher()

    with _BienNgoai():
        r = _tai_len(client, h)
    job_id = r.json()["job_id"]

    md_sua = MARKDOWN_DE.replace("Tính giá trị của $2^5$.", "NOI_DUNG_NGUOI_DUNG_DA_SUA")
    luu = client.patch(f"{API}/parser/{job_id}/ocr", json={"markdown": md_sua}, headers=h)
    assert luu.status_code == 200, luu.text

    cau_theo_ban_sua = [dict(_cau_hoi_gemini()[0], question="NOI_DUNG_NGUOI_DUNG_DA_SUA")]

    with _BienNgoai(cau_hoi=cau_theo_ban_sua) as _bn:
        p = client.post(f"{API}/parser/{job_id}/ocr/parse", headers=h)
    assert p.status_code == 200, p.text

    d = client.get(f"{API}/parser/status/{job_id}", headers=h).json()
    assert d["status"] == "completed", f"reparse không tới đích: {d}"

    noi_dung = {q["question_text"] for q in client.get(
        f"{API}/questions", params={"my_only": "true", "page_size": 100},
        headers=h).json()["items"]}
    assert "NOI_DUNG_NGUOI_DUNG_DA_SUA" in noi_dung


def test_parse_lai_khong_nhan_doi_cau_trong_ngan_hang(client, make_teacher):
    """Bấm 'Tạo câu hỏi' hai lần không được nhân đôi ngân hàng."""
    _, h = make_teacher()

    with _BienNgoai():
        r = _tai_len(client, h)
    job_id = r.json()["job_id"]

    with _BienNgoai():
        client.post(f"{API}/parser/{job_id}/ocr/parse", headers=h)
    lan1 = client.get(f"{API}/questions", params={"my_only": "true"}, headers=h).json()["total"]

    with _BienNgoai():
        client.post(f"{API}/parser/{job_id}/ocr/parse", headers=h)
    lan2 = client.get(f"{API}/questions", params={"my_only": "true"}, headers=h).json()["total"]

    assert lan1 == 3
    assert lan2 == 3, f"parse lại làm ngân hàng phình từ {lan1} lên {lan2}"


# ═════════════════════════════════════════════════════════════
# Đường hỏng — pipeline phải thất bại TỬ TẾ, không treo
# ═════════════════════════════════════════════════════════════

def test_ocr_loi_thi_de_chuyen_trang_thai_that_bai(client, make_teacher, khong_review_markdown):
    """Người dùng phải thấy lỗi, không phải thanh tiến trình quay mãi."""
    _, h = make_teacher()

    with _BienNgoai(ocr_loi=RuntimeError("OCR engine sập")):
        r = _tai_len(client, h)
    job_id = r.json()["job_id"]

    d = client.get(f"{API}/parser/status/{job_id}", headers=h).json()
    assert d["status"] == "failed", f"OCR lỗi mà trạng thái là {d['status']}"
    assert d["error_message"], "thất bại mà không có thông báo lỗi cho người dùng"


def test_gemini_loi_thi_de_chuyen_trang_thai_that_bai(client, make_teacher, khong_review_markdown):
    _, h = make_teacher()

    with _BienNgoai(parse_loi=RuntimeError("Gemini hết quota")):
        r = _tai_len(client, h)
    job_id = r.json()["job_id"]

    d = client.get(f"{API}/parser/status/{job_id}", headers=h).json()
    assert d["status"] == "failed"
    assert client.get(f"{API}/questions", params={"my_only": "true"},
                      headers=h).json()["total"] == 0


def test_ocr_tra_ve_rong_thi_khong_lang_le_bao_thanh_cong(client, make_teacher, khong_review_markdown):
    """OCR ra text rỗng = tài liệu không đọc được. Báo 'completed' với 0 câu là
    lừa người dùng."""
    _, h = make_teacher()

    with _BienNgoai(ocr=_artifact_ocr(text="")):
        r = _tai_len(client, h)
    job_id = r.json()["job_id"]

    d = client.get(f"{API}/parser/status/{job_id}", headers=h).json()
    assert d["status"] in ("failed", "needs_review"), (
        f"OCR rỗng mà báo {d['status']} — người dùng tưởng thành công"
    )


def test_gemini_khong_tim_thay_cau_nao_thi_danh_dau_can_ra_soat(client, make_teacher, khong_review_markdown):
    """Tài liệu đọc được nhưng không tách được câu nào → cần người rà soát,
    không phải 'completed' im lặng."""
    _, h = make_teacher()

    with _BienNgoai(cau_hoi=[]):
        r = _tai_len(client, h)
    job_id = r.json()["job_id"]

    d = client.get(f"{API}/parser/status/{job_id}", headers=h).json()
    assert d["status"] == "needs_review", f"0 câu mà báo {d['status']}"


# ═════════════════════════════════════════════════════════════
# Chống trùng xuyên đề
# ═════════════════════════════════════════════════════════════

def test_trung_xuyen_de_duoc_luu_nhung_gan_co_de_ra_soat(client, make_teacher, khong_review_markdown):
    """Tải hai ĐỀ KHÁC NHAU có nội dung câu giống hệt.

    HÀNH VI THẬT (không phải bỏ qua): câu vẫn được LƯU nhưng gắn cờ
    `is_bank_duplicate=True`. Đây là chủ ý — giáo viên cần thấy cả hai để tự
    quyết giữ bản nào, thay vì hệ thống âm thầm nuốt mất câu của đề thứ hai.

    Khác với trùng TRONG CÙNG một lần nạp: chỗ đó mới thực sự bỏ qua
    (xem tests/test_question_bank.py).
    """
    _, h = make_teacher()

    with _BienNgoai(ocr=_artifact_ocr(file_hash="hash_de_lan_1")):
        _tai_len(client, h, ten="de-lan-1.pdf")
    sau_lan1 = client.get(f"{API}/questions", params={"my_only": "true"},
                          headers=h).json()["total"]
    assert sau_lan1 == 3

    with _BienNgoai(ocr=_artifact_ocr(file_hash="hash_de_lan_2")):
        _tai_len(client, h, ten="de-lan-2.pdf")

    ds = client.get(f"{API}/questions", params={"my_only": "true", "page_size": 100},
                    headers=h).json()
    assert ds["total"] == 6, f"kỳ vọng lưu cả hai bộ, thực tế {ds['total']}"

    # `is_bank_duplicate` KHÔNG có trong schema trả về của API (app/schemas/question.py)
    # nên phải đọc thẳng DB. Ghi lại đây như một quan sát: cờ này hiện chỉ tồn
    # tại trong cơ sở dữ liệu, giao diện chưa có đường nào nhìn thấy nó.
    import sqlite3

    con = sqlite3.connect("_pytest.db")
    try:
        ids = tuple(q["id"] for q in ds["items"])
        cho = ",".join("?" * len(ids))
        so_gan_co = con.execute(
            f"SELECT COUNT(*) FROM question WHERE id IN ({cho}) AND is_bank_duplicate = 1",
            ids,
        ).fetchone()[0]
    finally:
        con.close()

    assert so_gan_co == 3, (
        f"3 câu của đề thứ hai phải được gắn cờ trùng trong DB, thực tế {so_gan_co}"
    )


# ═════════════════════════════════════════════════════════════
# Cách ly giữa các giáo viên
# ═════════════════════════════════════════════════════════════

def test_cau_tu_de_cua_A_khong_lo_sang_B(client, make_teacher, khong_review_markdown):
    _, hA = make_teacher()
    _, hB = make_teacher()

    with _BienNgoai():
        _tai_len(client, hA)

    assert client.get(f"{API}/questions", params={"my_only": "true"},
                      headers=hB).json()["total"] == 0
    assert client.get(f"{API}/parser/history", headers=hB).json()["total"] == 0


def test_B_khong_xem_duoc_trang_thai_de_cua_A(client, make_teacher, khong_review_markdown):
    _, hA = make_teacher()
    _, hB = make_teacher()

    with _BienNgoai():
        r = _tai_len(client, hA)
    job_id = r.json()["job_id"]

    assert client.get(f"{API}/parser/status/{job_id}", headers=hB).status_code in (403, 404)
