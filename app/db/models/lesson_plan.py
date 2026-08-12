"""
Lesson plan (KHBD theo Công văn 5512) models.

Ba bảng:
  - Yccd            : "yêu cầu cần đạt" gốc theo chương trình GDPT 2018 (TT32),
                      mỗi row = 1 YCCĐ ở mức mạch/chủ đề. Đây là NEO chống bịa.
  - CurriculumYccd  : ánh xạ bài (curriculum.id) ↔ YCCĐ (yccd.id). Tái dùng bảng
                      `curriculum` sẵn có làm tầng "bài học" (đã seed Toán 6-12 KNTT)
                      thay vì tạo bảng `lesson` mới.
  - LessonPlan      : một bản KHBD đã sinh (JSON) gắn với 1 bài + 1 giáo viên.

Grounding path là DETERMINISTIC: bài → YCCĐ qua join `curriculum_yccd` (SQL thuần,
không vector). Vector chỉ dùng ở bước retrieve câu hỏi mẫu (document_rag).
"""

from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, ForeignKey,
    Index, UniqueConstraint,
)
from datetime import datetime, timezone

from app.db.base_class import Base


class Yccd(Base):
    """Một 'yêu cầu cần đạt' gốc (GDPT 2018 / Thông tư 32)."""
    __tablename__ = "yccd"

    id           = Column(Integer, primary_key=True, index=True)
    # Mã tự đặt, ổn định để truy vết grounding, vd "TOAN6.PHANSO.04"
    code         = Column(String(80), unique=True, nullable=False)
    subject_code = Column(String(30), ForeignKey("subject.subject_code"), nullable=False, default="toan")
    grade        = Column(Integer, nullable=False)              # 1-12
    strand       = Column(String(150), nullable=False, default="")   # mạch nội dung, vd "Số và Đại số"
    topic        = Column(String(300), nullable=False, default="")   # chủ đề, vd "Phân số"
    requirement  = Column(Text, nullable=False)                 # văn bản YCCĐ NGUYÊN VĂN
    is_active    = Column(Boolean, default=True, nullable=False)

    __table_args__ = (
        Index("ix_yccd_subject_grade", "subject_code", "grade"),
        Index("ix_yccd_grade", "grade"),
    )


class CurriculumYccd(Base):
    """Ánh xạ bài học (curriculum) ↔ yêu cầu cần đạt (yccd)."""
    __tablename__ = "curriculum_yccd"

    id            = Column(Integer, primary_key=True, index=True)
    curriculum_id = Column(Integer, ForeignKey("curriculum.id", ondelete="CASCADE"), nullable=False)
    yccd_id       = Column(Integer, ForeignKey("yccd.id", ondelete="CASCADE"), nullable=False)
    # Nguồn ánh xạ: "manual" (nhập tay) | "llm" (gợi ý bán tự động, chưa duyệt) | "reviewed" (đã duyệt)
    source        = Column(String(20), nullable=False, default="manual")

    __table_args__ = (
        UniqueConstraint("curriculum_id", "yccd_id", name="uq_curriculum_yccd"),
        Index("ix_curriculum_yccd_curriculum", "curriculum_id"),
        Index("ix_curriculum_yccd_yccd", "yccd_id"),
    )


class LessonPlan(Base):
    """Một bản Kế hoạch bài dạy (KHBD) đã sinh cho 1 bài + 1 giáo viên."""
    __tablename__ = "lesson_plan"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(Integer, ForeignKey("user.id"), nullable=False)
    # Bài học nguồn (NULL nếu bài bị xóa khỏi curriculum)
    curriculum_id = Column(Integer, ForeignKey("curriculum.id", ondelete="SET NULL"), nullable=True)

    title         = Column(String(300), nullable=False, default="")
    subject_code  = Column(String(30), nullable=False, default="toan")
    grade         = Column(Integer, nullable=True)
    bo_sach       = Column(String(60), nullable=False, default="Kết nối tri thức")
    so_tiet       = Column(Integer, nullable=False, default=1)

    # Nội dung KHBD dạng JSON (khớp KHBD_SCHEMA) — nguồn sự thật để render Word
    content_json  = Column(Text, nullable=False, default="{}")

    # draft (giáo viên đang sửa) | generated (vừa sinh) | reviewed (đã duyệt, sẵn sàng xuất)
    status        = Column(String(20), nullable=False, default="generated")
    # Template render Word: "cv5512" (khung chuẩn Bộ) | "school" (template trường upload)
    template      = Column(String(40), nullable=False, default="cv5512")
    model_used    = Column(String(40), nullable=True)

    # Nhãn nguồn gốc nội dung (Điều 44 Luật CN CNS + Luật AI).
    # Xem app/core/content_origin.py: HUMAN | AI_GENERATED | AI_ASSISTED | OCR_IMPORT
    # ai_model trùng nghĩa model_used (giữ cả hai để đồng nhất với Question/Exam).
    origin           = Column(String(20), nullable=False, default="HUMAN", server_default="HUMAN")
    ai_model         = Column(String(60), nullable=True)
    reviewed_by_user = Column(Boolean, default=False, nullable=False, server_default='false')
    reviewed_at      = Column(DateTime(timezone=True), nullable=True)

    created_at    = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at    = Column(DateTime(timezone=True),
                           default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("ix_lesson_plan_user_created", "user_id", "created_at"),
        Index("ix_lesson_plan_curriculum", "curriculum_id"),
    )
