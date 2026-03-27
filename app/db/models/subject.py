"""
Subject model — danh sách các môn học K12 Việt Nam (GDPT 2018).
Dùng subject_code (string PK) thay vì integer để AI parser dễ output,
self-documenting khi đọc DB, và stable across environments.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Integer, Boolean, DateTime, ForeignKey, Index,
)
from app.db.base_class import Base


class Subject(Base):
    __tablename__ = "subject"

    subject_code  = Column(String(30), primary_key=True)
    name_vi       = Column(String(200), nullable=False)
    name_short    = Column(String(50), nullable=False)
    name_en       = Column(String(200), nullable=True)
    category      = Column(String(50), nullable=False)          # bat_buoc | lua_chon | tich_hop
    grade_min     = Column(Integer, nullable=False)
    grade_max     = Column(Integer, nullable=False)
    parent_code   = Column(String(30), ForeignKey("subject.subject_code"), nullable=True)
    display_order = Column(Integer, default=0)
    icon          = Column(String(50), nullable=True)
    is_active     = Column(Boolean, default=True)
    created_at    = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_subject_category", "category"),
        Index("ix_subject_grade_range", "grade_min", "grade_max"),
    )


# ─── GDPT 2018 — Danh sách môn học K12 ─────────────────────────────────────────
# Nguồn: Chương trình GDPT 2018, Bộ GD&ĐT
# grade_min/grade_max: phạm vi lớp mà môn này tồn tại
# parent_code: môn tích hợp cha (KHTN → Lý/Hóa/Sinh, LS&ĐL → Sử/Địa, v.v.)

SUBJECTS_GDPT_2018: list[dict] = [
    # ── Cốt lõi ──────────────────────────────────────────────────────────────
    {"subject_code": "toan",        "name_vi": "Toán",                          "name_short": "Toán",   "name_en": "Mathematics",       "category": "bat_buoc", "grade_min": 1,  "grade_max": 12, "parent_code": None, "display_order": 1,  "icon": "calculator"},
    {"subject_code": "tieng-viet",  "name_vi": "Tiếng Việt",                    "name_short": "TV",     "name_en": "Vietnamese",         "category": "bat_buoc", "grade_min": 1,  "grade_max": 5,  "parent_code": None, "display_order": 2,  "icon": "book-open"},
    {"subject_code": "ngu-van",     "name_vi": "Ngữ văn",                       "name_short": "Văn",    "name_en": "Literature",         "category": "bat_buoc", "grade_min": 6,  "grade_max": 12, "parent_code": None, "display_order": 3,  "icon": "book-open"},
    {"subject_code": "tieng-anh",   "name_vi": "Tiếng Anh",                     "name_short": "TA",     "name_en": "English",            "category": "bat_buoc", "grade_min": 3,  "grade_max": 12, "parent_code": None, "display_order": 4,  "icon": "globe"},

    # ── Khoa học tự nhiên ─────────────────────────────────────────────────────
    {"subject_code": "tnxh",        "name_vi": "Tự nhiên và Xã hội",            "name_short": "TNXH",   "name_en": "Nature & Society",   "category": "tich_hop", "grade_min": 1,  "grade_max": 3,  "parent_code": None,   "display_order": 10, "icon": "leaf"},
    {"subject_code": "khoa-hoc",    "name_vi": "Khoa học",                      "name_short": "KH",     "name_en": "Science",            "category": "bat_buoc", "grade_min": 4,  "grade_max": 5,  "parent_code": "tnxh", "display_order": 11, "icon": "flask"},
    {"subject_code": "khtn",        "name_vi": "Khoa học tự nhiên",             "name_short": "KHTN",   "name_en": "Natural Sciences",   "category": "tich_hop", "grade_min": 6,  "grade_max": 9,  "parent_code": None,   "display_order": 12, "icon": "atom"},
    {"subject_code": "vat-li",      "name_vi": "Vật lí",                        "name_short": "Lý",     "name_en": "Physics",            "category": "lua_chon", "grade_min": 10, "grade_max": 12, "parent_code": "khtn", "display_order": 13, "icon": "zap"},
    {"subject_code": "hoa-hoc",     "name_vi": "Hóa học",                       "name_short": "Hóa",    "name_en": "Chemistry",          "category": "lua_chon", "grade_min": 10, "grade_max": 12, "parent_code": "khtn", "display_order": 14, "icon": "flask"},
    {"subject_code": "sinh-hoc",    "name_vi": "Sinh học",                      "name_short": "Sinh",   "name_en": "Biology",            "category": "lua_chon", "grade_min": 10, "grade_max": 12, "parent_code": "khtn", "display_order": 15, "icon": "dna"},

    # ── Lịch sử & Địa lí ─────────────────────────────────────────────────────
    {"subject_code": "ls-dl",       "name_vi": "Lịch sử và Địa lí",            "name_short": "LS&ĐL",  "name_en": "History & Geography","category": "bat_buoc", "grade_min": 4,  "grade_max": 9,  "parent_code": None,   "display_order": 20, "icon": "map"},
    {"subject_code": "lich-su",     "name_vi": "Lịch sử",                      "name_short": "Sử",     "name_en": "History",            "category": "bat_buoc", "grade_min": 10, "grade_max": 12, "parent_code": "ls-dl","display_order": 21, "icon": "clock"},
    {"subject_code": "dia-li",      "name_vi": "Địa lí",                       "name_short": "Địa",    "name_en": "Geography",          "category": "lua_chon", "grade_min": 10, "grade_max": 12, "parent_code": "ls-dl","display_order": 22, "icon": "map-pin"},

    # ── Giáo dục công dân ─────────────────────────────────────────────────────
    {"subject_code": "dao-duc",     "name_vi": "Đạo đức",                      "name_short": "ĐĐ",     "name_en": "Ethics",             "category": "bat_buoc", "grade_min": 1,  "grade_max": 5,  "parent_code": None,   "display_order": 30, "icon": "heart"},
    {"subject_code": "gdcd",        "name_vi": "Giáo dục công dân",            "name_short": "GDCD",   "name_en": "Civic Education",    "category": "bat_buoc", "grade_min": 6,  "grade_max": 9,  "parent_code": None,   "display_order": 31, "icon": "shield"},
    {"subject_code": "gdktpl",      "name_vi": "GD Kinh tế và Pháp luật",      "name_short": "KT&PL",  "name_en": "Economics & Law",    "category": "lua_chon", "grade_min": 10, "grade_max": 12, "parent_code": "gdcd", "display_order": 32, "icon": "scale"},

    # ── Công nghệ & Tin học ───────────────────────────────────────────────────
    {"subject_code": "tin-hoc",     "name_vi": "Tin học",                       "name_short": "Tin",    "name_en": "Informatics",        "category": "lua_chon", "grade_min": 3,  "grade_max": 12, "parent_code": None, "display_order": 40, "icon": "monitor"},
    {"subject_code": "cong-nghe",   "name_vi": "Công nghệ",                    "name_short": "CN",     "name_en": "Technology",         "category": "lua_chon", "grade_min": 3,  "grade_max": 12, "parent_code": None, "display_order": 41, "icon": "wrench"},

    # ── Thể chất & Nghệ thuật ────────────────────────────────────────────────
    {"subject_code": "gdtc",        "name_vi": "Giáo dục thể chất",            "name_short": "GDTC",   "name_en": "Physical Education", "category": "bat_buoc", "grade_min": 1,  "grade_max": 12, "parent_code": None, "display_order": 50, "icon": "activity"},
    {"subject_code": "am-nhac",     "name_vi": "Âm nhạc",                      "name_short": "Nhạc",   "name_en": "Music",              "category": "lua_chon", "grade_min": 1,  "grade_max": 12, "parent_code": None, "display_order": 51, "icon": "music"},
    {"subject_code": "my-thuat",    "name_vi": "Mỹ thuật",                     "name_short": "MT",     "name_en": "Fine Arts",          "category": "lua_chon", "grade_min": 1,  "grade_max": 12, "parent_code": None, "display_order": 52, "icon": "palette"},

    # ── Hoạt động & Quốc phòng ───────────────────────────────────────────────
    {"subject_code": "hdtn",        "name_vi": "Hoạt động trải nghiệm",        "name_short": "HĐTN",   "name_en": "Experiential Activities","category": "bat_buoc","grade_min": 1, "grade_max": 12, "parent_code": None, "display_order": 60, "icon": "compass"},
    {"subject_code": "gdqpan",      "name_vi": "GD Quốc phòng và An ninh",     "name_short": "QP-AN",  "name_en": "Defense & Security", "category": "bat_buoc", "grade_min": 10, "grade_max": 12, "parent_code": None, "display_order": 61, "icon": "shield"},
]
