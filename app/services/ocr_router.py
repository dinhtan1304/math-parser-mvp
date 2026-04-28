"""
OCR Router — Chọn OCR backend theo môn học.

Mỗi môn học có đặc thù riêng:
- Toán/Lý/Hoá cần LaTeX → Pix2Text
- Sinh/KHTN/Địa lý cần layout + hình → MinerU
- Văn/Sử/GDCD chỉ cần text → PyMuPDF (nhanh nhất)
"""

from enum import Enum
from dataclasses import dataclass


class OCRBackend(Enum):
    PYMUPDF = "pymupdf"
    PIX2TEXT = "pix2text"
    MINERU = "mineru"


@dataclass
class OCRConfig:
    backend: OCRBackend
    lang: str = "vi,en"
    extract_images: bool = False
    parallel_pages: bool = True
    reason: str = ""


SUBJECT_OCR_MAP: dict[str, OCRConfig] = {
    "toan":      OCRConfig(OCRBackend.PIX2TEXT, extract_images=True,  reason="LaTeX nặng"),
    "vat-li":    OCRConfig(OCRBackend.PIX2TEXT, extract_images=True,  reason="Vector, sơ đồ mạch"),
    "hoa-hoc":   OCRConfig(OCRBackend.PIX2TEXT, extract_images=True,  reason="Chemical formula"),
    "sinh-hoc":  OCRConfig(OCRBackend.MINERU,   extract_images=True,  reason="Hình vẽ tế bào nhiều"),
    "khtn":      OCRConfig(OCRBackend.MINERU,   extract_images=True,  reason="Tổng hợp cần layout"),
    "dia-li":    OCRConfig(OCRBackend.MINERU,   extract_images=True,  reason="Bản đồ, biểu đồ"),
    "ngu-van":   OCRConfig(OCRBackend.PYMUPDF,  extract_images=False, reason="Text thuần"),
    "tieng-anh": OCRConfig(OCRBackend.PYMUPDF,  extract_images=False, reason="Text Latin"),
    "lich-su":   OCRConfig(OCRBackend.PYMUPDF,  extract_images=False, reason="Text thuần"),
    "gdcd":      OCRConfig(OCRBackend.PYMUPDF,  extract_images=False, reason="Text thuần"),
    "gdktpl":    OCRConfig(OCRBackend.PYMUPDF,  extract_images=False, reason="Text thuần"),
    "tin-hoc":   OCRConfig(OCRBackend.PYMUPDF,  extract_images=False, reason="Code block"),
    "ielts":     OCRConfig(OCRBackend.PYMUPDF,  extract_images=False, reason="Text Latin, no LaTeX"),
}

DEFAULT_OCR_CONFIG = OCRConfig(OCRBackend.PIX2TEXT, extract_images=True, reason="Default fallback")


def get_ocr_config(subject_code: str) -> OCRConfig:
    """Trả về OCR config phù hợp cho môn học."""
    return SUBJECT_OCR_MAP.get(subject_code, DEFAULT_OCR_CONFIG)
