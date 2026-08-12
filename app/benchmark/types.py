from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


OcrStatus = Literal["success", "failed", "skipped"]


@dataclass
class OcrAsset:
    type: str = "unknown"
    path: str = ""
    page: int | None = None
    bbox: list[float] | None = None
    caption: str | None = None


@dataclass
class OcrMetadata:
    page_count: int = 0
    detected_language: str | None = None
    has_math: bool = False
    has_tables: bool = False
    has_figures: bool = False


@dataclass
class OcrResult:
    engine: str
    status: OcrStatus
    error: str | None
    latency_ms: int
    markdown: str
    raw: dict[str, Any] = field(default_factory=dict)
    assets: list[OcrAsset] = field(default_factory=list)
    metadata: OcrMetadata = field(default_factory=OcrMetadata)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OcrResult":
        return cls(
            engine=str(payload.get("engine") or "unknown"),
            status=payload.get("status") or "failed",
            error=payload.get("error"),
            latency_ms=int(payload.get("latency_ms") or 0),
            markdown=str(payload.get("markdown") or ""),
            raw=dict(payload.get("raw") or {}),
            assets=[OcrAsset(**asset) for asset in payload.get("assets", [])],
            metadata=OcrMetadata(**dict(payload.get("metadata") or {})),
        )


@dataclass
class FileClassification:
    file_id: str
    path: str
    extension: str
    page_count: int = 0
    has_text_layer: bool = False
    pdf_kind: str = "unknown"
    recommended_ocr_mode: str = "auto"
    text_page_ratio: float = 0.0
    scan_page_ratio: float = 0.0
    document_type: str = "unknown"
    subject: str = "unknown"
    grade: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BenchmarkMetricRow:
    file_id: str
    file_path: str
    engine: str
    status: str
    latency_ms: int
    markdown_length: int
    plain_text_length: int
    vietnamese_char_ratio: float
    broken_unicode_count: int
    formula_count: int
    latex_valid_count: int
    latex_invalid_count: int
    latex_valid_ratio: float
    table_count: int
    image_asset_count: int
    question_count: int
    option_group_count: int
    heading_count: int
    page_count: int
    avg_line_length: float
    repeated_line_ratio: float
    markdown_cleanliness_score: float
    structure_score: float
    final_quality_score: float
    # Sprint 3 — 5 metric mới
    # image_quality_score: 0-100 — so với GT (NaN/0 nếu không có GT)
    image_quality_score: float = 0.0
    # markdown_image_link_count: số ![](file_path) trong markdown (KHÔNG phải base64)
    markdown_image_link_count: int = 0
    # markdown_image_base64_count: số data:image/...;base64 inline
    markdown_image_base64_count: int = 0
    # latex_complexity_score: 0-100 — ratio LaTeX commands "phức tạp" trên total commands
    # Phức tạp: \frac, \int, \sum, \begin{matrix|cases|align|pmatrix|...}, \prod, \lim, ...
    latex_complexity_score: float = 0.0
    # vn_diacritic_accuracy: 0-1 — proxy chất lượng OCR VN qua ratio diacritic + ascii letters
    vn_diacritic_accuracy: float = 0.0
    document_type: str = "unknown"
    subject: str = "unknown"
    grade: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
