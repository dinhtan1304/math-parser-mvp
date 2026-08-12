"""Shared helpers for hybrid upload ingest.

The parser endpoints still own domain-specific parsing. This module owns the
document-RAG branch and the small metadata contract used by status/SSE.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def normalize_warnings(warnings: list[str] | None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for warning in warnings or []:
        text = str(warning).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def merge_ingest_metadata(
    payload: Any,
    *,
    warnings: list[str] | None = None,
    ingest_stats: dict[str, Any] | None = None,
    payload_type: str | None = None,
) -> str:
    """Return a result_json string with optional ingest metadata.

    IELTS already stores an object payload. K12 historically stored a raw list;
    wrap it so metadata can travel without adding new DB columns.
    """
    clean_warnings = normalize_warnings(warnings)
    stats = ingest_stats or {}

    if isinstance(payload, dict):
        result = dict(payload)
    else:
        result = {
            "type": payload_type or "k12",
            "questions": payload,
        }

    # Phase 3.13: schema_version để migrate an toàn khi đổi field result_json sau này.
    result.setdefault("schema_version", 1)
    if clean_warnings:
        result["warnings"] = clean_warnings
    if stats:
        result["ingest_stats"] = stats

    return json.dumps(result, ensure_ascii=False)


def extract_ingest_metadata(result_json: str | None) -> tuple[list[str], dict[str, Any]]:
    if not result_json:
        return [], {}
    try:
        parsed = json.loads(result_json)
    except Exception:
        return [], {}
    if not isinstance(parsed, dict):
        return [], {}
    warnings = parsed.get("warnings")
    stats = parsed.get("ingest_stats")
    return normalize_warnings(warnings if isinstance(warnings, list) else []), (
        stats if isinstance(stats, dict) else {}
    )


def extract_questions_payload(result_json: str | None) -> list[dict] | None:
    """Read K12 questions from legacy list or wrapped result_json."""
    if not result_json:
        return None
    try:
        parsed = json.loads(result_json)
    except Exception:
        return None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("questions"), list):
        return parsed["questions"]
    return None


async def index_document_for_rag(
    db: AsyncSession,
    *,
    file_path: str,
    exam_id: int,
    user_id: int,
    subject_code: str | None,
    grade: int | None = None,
    markdown_override: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Run the question-aware chunk + embed branch.

    Never raises to callers. The upload parse branch is the primary output, so
    failures are represented as warnings and stats. ``markdown_override`` (OCR
    review resume) re-indexes from the user-edited markdown.
    """
    stats: dict[str, Any] = {
        "document_chunks": 0,
        "rag_index_status": "failed",
    }
    warnings: list[str] = []

    try:
        from app.services.pipeline import step1_5_chunk_and_embed

        chunk_count = await step1_5_chunk_and_embed(
            db,
            file_path,
            exam_id,
            user_id,
            subject_code=subject_code,
            grade=grade,
            markdown_override=markdown_override,
        )
        stats["document_chunks"] = int(chunk_count or 0)
        if chunk_count:
            stats["rag_index_status"] = "completed"
        else:
            stats["rag_index_status"] = "warning"
            warnings.append(
                "Không tạo được document chunks cho RAG. File vẫn được phân tích thành câu hỏi."
            )
    except Exception as exc:
        logger.warning("Document RAG index failed for exam %s: %s", exam_id, exc)
        stats["rag_index_status"] = "failed"
        warnings.append(f"Index tài liệu RAG thất bại: {exc}")

    return stats, normalize_warnings(warnings)
