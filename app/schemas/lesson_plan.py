"""Pydantic schemas cho tính năng KHBD (Kế hoạch bài dạy CV5512)."""

from __future__ import annotations
from typing import Any, Dict, List, Optional
from datetime import datetime
from pydantic import BaseModel, Field


# ── Dropdown: bài học ───────────────────────────────────────────────────────

class LessonOption(BaseModel):
    """Một bài học (curriculum) cho dropdown chọn bài."""
    curriculum_id: int
    chapter_no: int
    chapter: str
    lesson_no: int
    lesson_title: str
    has_yccd: bool = Field(description="Bài đã được ánh xạ yêu cầu cần đạt chưa")


class LessonListResponse(BaseModel):
    grade: int
    subject_code: str
    bo_sach: str = "Kết nối tri thức"
    lessons: List[LessonOption]


# ── Sinh KHBD ───────────────────────────────────────────────────────────────

class GenerateKhbdRequest(BaseModel):
    curriculum_id: int
    so_tiet: Optional[int] = Field(default=None, ge=1, le=10)
    template: str = Field(default="cv5512", description="cv5512 | school")
    use_cache: bool = True


class LessonPlanOut(BaseModel):
    id: int
    title: str
    subject_code: str
    grade: Optional[int] = None
    bo_sach: str
    so_tiet: int
    status: str
    template: str
    model_used: Optional[str] = None
    content: Dict[str, Any] = Field(description="Nội dung KHBD (meta + muc_tieu + thiet_bi + tien_trinh)")
    warnings: List[str] = []
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Nhãn nguồn gốc nội dung — FE hiển thị badge AI (Điều 44 Luật CN CNS)
    origin: str = "HUMAN"
    reviewed_by_user: bool = False


class LessonPlanSummary(BaseModel):
    id: int
    title: str
    subject_code: str
    grade: Optional[int] = None
    so_tiet: int
    status: str
    template: str
    created_at: Optional[datetime] = None
    # Nhãn nguồn gốc nội dung — FE hiển thị badge AI (Điều 44 Luật CN CNS)
    origin: str = "HUMAN"
    reviewed_by_user: bool = False


class LessonPlanListResponse(BaseModel):
    items: List[LessonPlanSummary]
    total: int


class UpdateKhbdRequest(BaseModel):
    """Giáo viên sửa nội dung / tiêu đề / trạng thái trước khi xuất."""
    title: Optional[str] = Field(default=None, max_length=300)
    content: Optional[Dict[str, Any]] = None
    status: Optional[str] = Field(default=None, description="draft | generated | reviewed")
    template: Optional[str] = None
