"""
Schema dùng chung cho các luồng parse (K12 và IELTS).

`ParseResponse` trước đây định nghĩa trong `app/api/parser.py` và
`app/api/ielts_parser.py` phải import chéo từ router sang router. Chuyển về
đây (2026-08-12) để hai router cùng phụ thuộc vào tầng schema, không phụ thuộc
lẫn nhau.
"""

from pydantic import BaseModel


class ParseResponse(BaseModel):
    """Phản hồi khi nhận file: job đã được xếp hàng, theo dõi tiếp qua SSE."""

    job_id: int
    status: str
    message: str
