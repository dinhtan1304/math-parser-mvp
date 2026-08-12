"""
Đường dẫn thư mục lưu file tải lên — dùng chung cho mọi luồng parse.

Tách khỏi `app/api/parser.py` (2026-08-12) để `api/ielts_parser.py` không phải
import hằng số từ router khác. Hai router PHẢI ghi vào cùng một thư mục, nếu
không thì dọn file theo `UPLOAD_RETENTION_DAYS` sẽ bỏ sót một nửa.

Cấu trúc:
    uploads/                 file gốc người dùng tải lên (bị dọn theo retention)
    uploads/ocr_artifacts/   cache kết quả OCR theo file_hash (KHÔNG dọn —
                             xóa đi là mất cache, tải lại phải OCR lại từ đầu)
    uploads/ocr_review/      markdown người dùng đang sửa ở bước duyệt OCR
"""

import os

UPLOAD_DIR = "uploads"
OCR_REVIEW_DIR = os.path.join(UPLOAD_DIR, "ocr_review")

os.makedirs(UPLOAD_DIR, exist_ok=True)
