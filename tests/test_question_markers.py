"""
Test đặc tả cho `app/services/question_markers.py`.

Bộ nhận diện ranh giới câu hỏi này là điểm chung của HAI luồng:
  - `pipeline.py`        — tách đề thành từng câu để lưu ngân hàng
  - `docling_chunker.py` — cắt markdown thành chunk RAG (không cắt ngang câu)

Trước 2026-08-12 nó nằm trong `pipeline.py` và `docling_chunker` phải import
ngược lên, tạo vòng import. Test này khóa hành vi trước khi tách ra, để việc
tách chứng minh được là không đổi kết quả.

Chỉ regex trên chuỗi — không DB, không fixture.
"""

from app.services.question_markers import find_question_markers


def _positions(text):
    """(vị trí bắt đầu, số câu) của từng marker."""
    return [(m.start(), m.group(1)) for m in find_question_markers(text)]


# ─────────────────────────────────────────────────────────────
# Định dạng text thuần
# ─────────────────────────────────────────────────────────────

def test_bat_cau_thuong():
    assert _positions("Câu 1. Tính x\nCâu 2. Tìm y") == [(0, "1"), (13, "2")]


def test_bat_bai_nhu_bat_cau():
    assert _positions("Bài 1. abc\nBài 2) def") == [(0, "1"), (10, "2")]


def test_bat_dinh_dang_co_diem_trong_ngoac():
    """Đề HSG/Olympiad hay ghi điểm ngay sau số câu: 'Câu 2 (4,0 điểm)'.

    Đây là lý do dùng lookahead (?=\\D) thay vì bắt buộc có dấu . : ) ngay sau.
    """
    assert _positions("Câu 1 (4,0 điểm) Cho tam giác\nCâu 2: Giải") == [(0, "1"), (29, "2")]


def test_khong_co_cau_nao_tra_ve_rong():
    assert find_question_markers("Không có câu nào") == []


def test_chuoi_rong():
    assert find_question_markers("") == []


# ─────────────────────────────────────────────────────────────
# Định dạng markdown (bộ render Marker/MinerU thêm '#')
# ─────────────────────────────────────────────────────────────

def test_bat_tieu_de_markdown():
    assert _positions("## Câu 1\nnội dung\n## Câu 2\nnội dung") == [(0, "1"), (17, "2")]


def test_bat_tieu_de_markdown_co_in_dam():
    assert _positions("## **Câu 3** nội dung") == [(0, "3")]


def test_markdown_thang_khi_trung_vi_tri():
    """Hai bộ cùng khớp một chỗ → lấy bản markdown.

    Quan trọng: bản markdown bao trọn cả dấu '#', nên cắt theo nó mới không để
    sót ký tự tiêu đề rơi vào cuối câu trước.
    """
    ms = find_question_markers("## Câu 1\nnội dung")
    assert len(ms) == 1
    assert ms[0].group(0).lstrip("\n").lstrip().startswith("#")


def test_tron_markdown_va_text_thuan():
    assert _positions("Câu 1. A\n## Câu 1\nB") == [(0, "1"), (8, "1")]


def test_ket_qua_luon_sap_theo_vi_tri():
    ms = find_question_markers("Câu 1. A\n## Câu 2\nB\nCâu 3. C\n### Câu 4\nD")
    vi_tri = [m.start() for m in ms]
    assert vi_tri == sorted(vi_tri)
    assert [m.group(1) for m in ms] == ["1", "2", "3", "4"]


# ─────────────────────────────────────────────────────────────
# Chống bắt nhầm
# ─────────────────────────────────────────────────────────────

def test_khong_bat_khi_khong_dung_dau_dong():
    """'Câu' giữa câu văn không phải ranh giới câu hỏi."""
    assert find_question_markers("Xem lại Câu 5 ở trên") == []


def test_hai_chu_so_van_bat_dung_so():
    assert _positions("Câu 12. abc") == [(0, "12")]


# ─────────────────────────────────────────────────────────────
# Không còn vòng import
# ─────────────────────────────────────────────────────────────

def _import_thuc_te(duong_dan: str) -> list[str]:
    """Các module được import THẬT SỰ (phân tích AST, bỏ qua docstring/comment)."""
    import ast
    import pathlib

    cay = ast.parse(pathlib.Path(duong_dan).read_text(encoding="utf-8"))
    ten = []
    for node in ast.walk(cay):
        if isinstance(node, ast.Import):
            ten += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            ten.append(node.module)
    return ten


def test_module_khong_phu_thuoc_module_nao_trong_app():
    """question_markers là tầng thấp nhất: chỉ regex, không import app.*

    Nếu ai đó thêm import từ app/ vào đây, vòng tròn có thể quay lại.
    """
    tu_app = [m for m in _import_thuc_te("app/services/question_markers.py")
              if m.startswith("app")]
    assert not tu_app, f"question_markers không được import app.*: {tu_app}"


def test_docling_chunker_khong_con_import_nguoc_len_pipeline():
    """Đây chính là vòng import đã phá.

    docling_chunker ← pipeline (pipeline import docling_chunker) VÀ
    docling_chunker → pipeline (lấy _find_question_markers) = vòng thật.
    Nay cả hai cùng import xuống question_markers.
    """
    assert "app.services.pipeline" not in _import_thuc_te("app/services/docling_chunker.py"), (
        "docling_chunker lại import ngược lên pipeline — vòng import quay lại"
    )


def test_pipeline_va_docling_chunker_dung_chung_mot_bo_nhan_dien():
    """Hai luồng phải cắt câu giống hệt nhau.

    Nếu lệch, chunk RAG sẽ cắt ngang câu mà ngân hàng lại tách đúng (hoặc
    ngược lại) — sai lệch âm thầm, không có lỗi nào báo.
    """
    from app.services import docling_chunker, pipeline
    from app.services.question_markers import find_question_markers

    assert pipeline._find_question_markers is find_question_markers
    assert docling_chunker.find_question_markers is find_question_markers
