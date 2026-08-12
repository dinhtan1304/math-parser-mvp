"""Nhãn nguồn gốc nội dung — tuân thủ Điều 44 Luật Công nghiệp công nghệ số và
Luật Trí tuệ nhân tạo (gắn nhãn nội dung do AI tạo ra).

Mọi nơi lưu Question/Exam/LessonPlan PHẢI đặt ``origin`` (và ``ai_model`` khi
nội dung do AI sinh). Không để mặc định trôi qua khi biết rõ nguồn.
"""

# Người dùng tự soạn hoàn toàn.
HUMAN = "HUMAN"
# Do AI sinh ra, chưa có giáo viên duyệt.
AI_GENERATED = "AI_GENERATED"
# Do AI sinh/nhận dạng và đã được giáo viên xem xét, chỉnh sửa, chấp nhận.
AI_ASSISTED = "AI_ASSISTED"
# Trích xuất từ tài liệu người dùng tải lên qua OCR.
OCR_IMPORT = "OCR_IMPORT"

ALL_ORIGINS = (HUMAN, AI_GENERATED, AI_ASSISTED, OCR_IMPORT)

# Các origin cần gắn nhãn AI khi hiển thị và khi xuất file.
AI_ORIGINS = (AI_GENERATED, AI_ASSISTED, OCR_IMPORT)


def is_ai_origin(origin: str | None) -> bool:
    """True khi nội dung có sự tham gia của AI (kể cả OCR) → phải gắn nhãn."""
    return origin in AI_ORIGINS
