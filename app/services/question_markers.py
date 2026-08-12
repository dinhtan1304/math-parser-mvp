"""
Nhận diện ranh giới câu hỏi ("Câu 1", "## Bài 2", …) trong markdown/text OCR.

TẠI SAO MODULE NÀY TỒN TẠI
Trước 2026-08-12, `_find_question_markers` nằm trong `app/services/pipeline.py`,
còn `docling_chunker.py` phải import ngược lên nó:

    # docling_chunker.py
    from app.services.pipeline import _find_question_markers   # ❌ vòng tròn

Trong khi `pipeline.py` cũng import `docling_chunker` → vòng import thật, đi qua
một tên private. Cả hai chiều đều defer trong hàm nên Python không nổ, nhưng
vòng vẫn có: sửa một bên phải nghĩ tới bên kia.

Đây là tầng THẤP NHẤT: chỉ regex trên chuỗi, không phụ thuộc module nào trong
`app/`. Cả `pipeline` lẫn `docling_chunker` đều import xuống đây, không ai import
ngang.

Test: tests/test_question_markers.py
"""

from __future__ import annotations

import re

# Sprint 6 A1: lookahead `(?=\D)` thay cho `\s*[.:\)]` để match được format
# "Câu 2 (4,0 điểm)" — đề thi HSG/Olympiad thường ghi điểm trong ngoặc.
# Match: "Câu 1.", "Câu 2 (4,0 điểm)", "Câu 3:", "Câu 4)", "Câu 5\n", "Bài 6 ".
# Không match: digit ngay sau "Câu N" (ví dụ "Câu 12" sẽ match cả số 12).
RE_QUESTION_SPLIT = re.compile(
    r'(?:^|\n)\s*(?:Câu|câu|Bài|bài|Question)\s+(\d+)(?=\D|$)',
    re.IGNORECASE,
)

# Bộ render markdown (Marker/MinerU) hay thêm tiền tố "## " / "### " cho tiêu đề
# câu, và đôi khi bọc thêm ** in đậm.
RE_MARKDOWN_QUESTION_SPLIT = re.compile(
    r'(?:^|\n)\s*#{1,6}\s*(?:\*{1,2}\s*)?(?:Câu|câu|Bài|bài|Question)\s+(\d+)(?=\D|$)',
    re.IGNORECASE,
)


def find_question_markers(text: str) -> list[re.Match]:
    """Trả về các vị trí bắt đầu câu hỏi, đã sắp theo thứ tự xuất hiện.

    Gộp hai bộ nhận diện: text thuần và markdown có tiêu đề. Khi hai bộ cùng
    khớp tại MỘT vị trí, bản markdown thắng (nó bao trọn cả dấu `#`, nên cắt
    theo nó mới không để sót ký tự tiêu đề vào cuối câu trước).

    Trả `[]` khi không tìm thấy gì — bên gọi tự quyết cách xử lý (pipeline rơi
    về bộ tách khác, docling_chunker rơi về chunk theo độ dài).
    """
    matches = list(RE_QUESTION_SPLIT.finditer(text))
    markdown_matches = list(RE_MARKDOWN_QUESTION_SPLIT.finditer(text))
    if not markdown_matches:
        return matches
    by_start = {m.start(): m for m in matches}
    for m in markdown_matches:
        by_start[m.start()] = m  # trùng vị trí → markdown ghi đè
    return [by_start[k] for k in sorted(by_start)]
