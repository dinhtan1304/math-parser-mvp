"""Nguyên tắc thu thập tối thiểu dữ liệu học sinh (Luật BVDLCN 91/2025).

Học sinh là người chưa thành niên. Ứng dụng chỉ được lưu định danh nội bộ + kết
quả bài làm; TUYỆT ĐỐI không thêm cột định danh cá nhân. Test này là hàng rào tự
động: thêm nhầm một cột như `date_of_birth` vào các bảng dưới đây sẽ làm CI đỏ.

Nếu một thay đổi hợp lệ thật sự cần thêm trường, phải cập nhật đồng thời:
  - content/legal/chinh-sach-bao-mat.md (mục 2.4 Dữ liệu học sinh)
  - Điều khoản sử dụng (mục 5)
và có cơ sở pháp lý rõ ràng — không sửa test cho qua.
"""
import pytest

from app.db.models.classroom import ClassMember, Submission, AnswerDetail
from app.db.models.quiz_attempt import QuizAttempt, QuizAnswer


# Tên cột bị cấm trên các bảng chứa dữ liệu học sinh.
FORBIDDEN_COLUMNS = {
    "date_of_birth", "dob", "birthday", "birth_date", "ngay_sinh",
    "phone", "phone_number", "mobile", "so_dien_thoai",
    "email", "student_email", "parent_email",
    "address", "home_address", "dia_chi",
    "photo", "photo_url", "avatar", "avatar_url", "anh",
    "id_number", "national_id", "cccd", "cmnd",
}

STUDENT_DATA_MODELS = [ClassMember, Submission, AnswerDetail, QuizAttempt, QuizAnswer]


@pytest.mark.parametrize("model", STUDENT_DATA_MODELS, ids=lambda m: m.__name__)
def test_student_tables_have_no_personal_identifiers(model):
    columns = {c.name.lower() for c in model.__table__.columns}
    violations = columns & FORBIDDEN_COLUMNS
    assert not violations, (
        f"{model.__name__} chứa cột định danh cá nhân của học sinh: {sorted(violations)}. "
        "Vi phạm nguyên tắc thu thập tối thiểu — xem docstring của test này."
    )


def test_submission_keeps_anonymization_marker():
    """Bài làm phải có mốc ẩn danh hóa để job retention gỡ liên kết danh tính."""
    assert "anonymized_at" in {c.name for c in Submission.__table__.columns}
    assert "anonymized_at" in {c.name for c in QuizAttempt.__table__.columns}
