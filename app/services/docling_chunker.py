"""Docling Chunker — Hierarchical document chunking for RAG.

Uses Docling's DocumentConverter + HierarchicalChunker to split
documents into semantically meaningful chunks that preserve:
- Section/chapter boundaries
- Table integrity
- Reading order
- Heading hierarchy as metadata

Install: pip install docling
First run downloads ~1-2GB of ML models.

Why Docling for chunking (not Marker)?
- Docling has built-in HierarchicalChunker that respects document structure
- Marker's output is flat Markdown — splitting by headers loses context
- Docling preserves semantic relationships between sections
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2)
_converter = None  # Lazy-loaded
_available: Optional[bool] = None


def is_available() -> bool:
    """Check if docling is installed."""
    global _available
    if _available is None:
        try:
            import docling  # noqa: F401
            _available = True
            logger.info("docling available")
        except ImportError:
            _available = False
            logger.info("docling not installed — Docling chunking disabled")
    return _available


def _get_converter():
    """Lazy-load Docling DocumentConverter."""
    global _converter
    if _converter is None:
        try:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter

            try:
                from docling.datamodel.pipeline_options import EasyOcrOptions
                ocr_options = EasyOcrOptions(force_full_page_ocr=False)
            except Exception:
                ocr_options = None

            pipeline_options = PdfPipelineOptions()
            pipeline_options.do_ocr = True
            pipeline_options.do_table_structure = True
            if ocr_options is not None:
                pipeline_options.ocr_options = ocr_options

            try:
                from docling.document_converter import PdfFormatOption
                _converter = DocumentConverter(
                    format_options={
                        InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
                    }
                )
            except Exception:
                _converter = DocumentConverter()
            logger.info("Docling converter loaded successfully")
        except Exception as e:
            logger.error(f"Failed to load Docling converter: {e}")
            raise
    return _converter


async def extract_text(file_path: str) -> dict:
    """Dùng Docling DocumentConverter để OCR file → markdown.

    Khác `chunk_document` ở chỗ chỉ trả raw markdown (không chunk). Dùng làm
    OCR fallback khi Marker fail trên scan tiếng Việt — Docling có EasyOCR
    built-in mạnh cho diacritics + table phức tạp.

    Returns:
        {
            "text": str (clean markdown),
            "page_count": int,
            "method": "docling",
            "image_map": dict (empty for now — Docling images extraction TBD),
        }
    """
    if not is_available():
        logger.info("Docling not installed, skipping OCR")
        return {"text": "", "page_count": 0, "method": "docling-not-installed", "image_map": {}}

    loop = asyncio.get_running_loop()

    def _extract():
        converter = _get_converter()
        result = converter.convert(file_path)
        doc = result.document

        try:
            markdown = doc.export_to_markdown()
        except Exception as exc:
            logger.warning("Docling export_to_markdown failed: %s", exc)
            markdown = ""

        page_count = 0
        try:
            pages = getattr(doc, "pages", None)
            if pages:
                page_count = len(pages) if hasattr(pages, "__len__") else 0
        except Exception:
            page_count = 0

        return markdown or "", page_count

    try:
        text, page_count = await loop.run_in_executor(_executor, _extract)
        logger.info(
            "Docling OCR complete: %d chars, %d pages from %s",
            len(text), page_count, file_path,
        )
        return {
            "text": text,
            "page_count": page_count,
            "method": "docling",
            "image_map": {},
        }
    except Exception as exc:
        logger.warning("Docling OCR failed: %s", exc)
        return {"text": "", "page_count": 0, "method": "docling-error", "image_map": {}}


async def chunk_document(
    file_path: str,
    max_chunk_tokens: int = 512,
) -> list[dict]:
    """Parse document and split into hierarchical chunks.

    Args:
        file_path: Path to PDF/DOCX/PPTX/HTML file.
        max_chunk_tokens: Max tokens per chunk (default 512 for optimal retrieval).

    Returns:
        [
            {
                "text": str,
                "index": int,
                "section_title": str,       # e.g. "Chương 2. Hằng đẳng thức"
                "page": int | None,
                "heading_trail": str,       # e.g. "Toán 8 > Chương 2 > Bài 1"
                "char_count": int,
            },
            ...
        ]
    """
    if not is_available():
        logger.warning("Docling not installed, returning empty chunks")
        return []

    loop = asyncio.get_running_loop()

    def _chunk():
        converter = _get_converter()
        result = converter.convert(file_path)
        doc = result.document

        # Use hierarchical chunker for structure-aware splitting
        try:
            from docling.chunking import HierarchicalChunker

            chunker = HierarchicalChunker(
                max_tokens=max_chunk_tokens,
                merge_peers=True,  # Merge small adjacent chunks
            )
            raw_chunks = list(chunker.chunk(doc))
        except Exception as e:
            logger.warning(f"HierarchicalChunker failed, using markdown split: {e}")
            # Fallback: split markdown by sections
            return _fallback_markdown_chunk(doc, max_chunk_tokens)

        chunks = []
        for i, chunk in enumerate(raw_chunks):
            # Extract text
            text = ""
            if hasattr(chunk, "text"):
                text = chunk.text
            elif isinstance(chunk, str):
                text = chunk
            else:
                text = str(chunk)

            # Skip trivially short chunks
            if not text or len(text.strip()) < 30:
                continue

            # Extract metadata
            section_title = ""
            page = None
            heading_trail = ""

            if hasattr(chunk, "meta") and chunk.meta:
                meta = chunk.meta
                # Docling provides heading hierarchy in various formats
                headings = []
                if isinstance(meta, dict):
                    headings = meta.get("headings", [])
                    page = meta.get("page", None)
                elif hasattr(meta, "headings"):
                    headings = getattr(meta, "headings", [])
                    page = getattr(meta, "page", None)

                if headings:
                    section_title = headings[-1] if isinstance(headings[-1], str) else str(headings[-1])
                    heading_trail = " > ".join(str(h) for h in headings)

            chunks.append({
                "text": text.strip(),
                "index": len(chunks),  # Use actual index after filtering
                "section_title": section_title,
                "page": page,
                "heading_trail": heading_trail,
                "char_count": len(text.strip()),
            })

        return chunks

    try:
        chunks = await loop.run_in_executor(_executor, _chunk)
        total_chars = sum(c["char_count"] for c in chunks)
        logger.info(
            f"Docling chunker: {len(chunks)} chunks, "
            f"{total_chars:,} total chars from {file_path}"
        )
        return chunks
    except Exception as e:
        logger.warning(f"Docling chunking failed: {e}")
        return []


def _fallback_markdown_chunk(doc, max_tokens: int = 512) -> list[dict]:
    """Fallback: split Docling document markdown by headers.

    Used when HierarchicalChunker fails (e.g. unsupported doc structure).
    """
    try:
        markdown = doc.export_to_markdown()
    except Exception:
        return []

    if not markdown:
        return []

    # Split by markdown headers (## or ###)
    import re
    sections = re.split(r'\n(?=#{1,3}\s)', markdown)

    chunks = []
    for section in sections:
        section = section.strip()
        if not section or len(section) < 30:
            continue

        # Extract section title from first line
        lines = section.split("\n", 1)
        title = lines[0].lstrip("#").strip() if lines[0].startswith("#") else ""

        # Split large sections further by paragraphs
        if len(section) > max_tokens * 4:  # Rough char-to-token ratio
            paragraphs = section.split("\n\n")
            current_chunk = ""
            for para in paragraphs:
                if len(current_chunk) + len(para) > max_tokens * 4:
                    if current_chunk:
                        chunks.append({
                            "text": current_chunk.strip(),
                            "index": len(chunks),
                            "section_title": title,
                            "page": None,
                            "heading_trail": title,
                            "char_count": len(current_chunk.strip()),
                        })
                    current_chunk = para
                else:
                    current_chunk += "\n\n" + para if current_chunk else para
            if current_chunk and len(current_chunk.strip()) >= 30:
                chunks.append({
                    "text": current_chunk.strip(),
                    "index": len(chunks),
                    "section_title": title,
                    "page": None,
                    "heading_trail": title,
                    "char_count": len(current_chunk.strip()),
                })
        else:
            chunks.append({
                "text": section,
                "index": len(chunks),
                "section_title": title,
                "page": None,
                "heading_trail": title,
                "char_count": len(section),
            })

    return chunks


async def chunk_markdown_text(
    markdown_text: str,
    max_chunk_chars: int = 2000,
) -> list[dict]:
    """Chunk raw markdown text (e.g. from Marker output) by headers/paragraphs.

    This is a lightweight alternative when Docling is not available
    or when you already have markdown from another OCR tool.

    Args:
        markdown_text: Raw markdown string.
        max_chunk_chars: Max characters per chunk.

    Returns:
        List of chunk dicts (same format as chunk_document).
    """
    import re

    if not markdown_text or len(markdown_text.strip()) < 30:
        return []

    # Split by markdown headers
    sections = re.split(r'\n(?=#{1,3}\s)', markdown_text)
    chunks = []

    for section in sections:
        section = section.strip()
        if not section or len(section) < 30:
            continue

        lines = section.split("\n", 1)
        title = lines[0].lstrip("#").strip() if lines[0].startswith("#") else ""

        if len(section) <= max_chunk_chars:
            chunks.append({
                "text": section,
                "index": len(chunks),
                "section_title": title,
                "page": None,
                "heading_trail": title,
                "char_count": len(section),
            })
        else:
            # Split large sections by double newline
            paragraphs = section.split("\n\n")
            current = ""
            for para in paragraphs:
                if len(current) + len(para) > max_chunk_chars and current:
                    chunks.append({
                        "text": current.strip(),
                        "index": len(chunks),
                        "section_title": title,
                        "page": None,
                        "heading_trail": title,
                        "char_count": len(current.strip()),
                    })
                    current = para
                else:
                    current += "\n\n" + para if current else para
            if current and len(current.strip()) >= 30:
                chunks.append({
                    "text": current.strip(),
                    "index": len(chunks),
                    "section_title": title,
                    "page": None,
                    "heading_trail": title,
                    "char_count": len(current.strip()),
                })

    logger.info(f"Markdown chunker: {len(chunks)} chunks from {len(markdown_text):,} chars")
    return chunks


# ── Question-aware chunker (đề thi VN) ──────────────────────────────────────
# Cắt theo ranh giới câu (Câu/Bài/Question) thay vì 2000 ký tự thô → 1 câu = 1
# chunk, KHÔNG cắt ngang công thức $$...$$ (mask trước khi cắt).
import re as _re

_FORMULA_RE = _re.compile(r'(\$\$[\s\S]+?\$\$|\$[^$\n]+?\$)')
_MASK_RE = _re.compile(r'@@F(\d+)@@')


def _mask_formulas(text: str) -> tuple[str, list[str]]:
    masks: list[str] = []

    def _repl(m):
        masks.append(m.group(0))
        return f"@@F{len(masks) - 1}@@"

    return _FORMULA_RE.sub(_repl, text), masks


def _unmask_formulas(text: str, masks: list[str]) -> str:
    def _repl(m):
        i = int(m.group(1))
        return masks[i] if 0 <= i < len(masks) else m.group(0)

    return _MASK_RE.sub(_repl, text)


def _split_oversized(seg: str, max_chars: int, overlap: int) -> list[str]:
    """Split an over-long segment at paragraph (\\n\\n) boundaries with whole-
    paragraph overlap. Never splits inside a masked formula placeholder."""
    if len(seg) <= max_chars:
        return [seg]
    paras = [p for p in seg.split("\n\n") if p.strip()]
    pieces: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for p in paras:
        if cur and cur_len + len(p) > max_chars:
            pieces.append("\n\n".join(cur).strip())
            if overlap and len(cur[-1]) <= overlap * 2:  # carry last para as overlap
                cur = [cur[-1], p]
                cur_len = len(cur[-1]) + len(p)
            else:
                cur = [p]
                cur_len = len(p)
        else:
            cur.append(p)
            cur_len += len(p)
    if cur:
        pieces.append("\n\n".join(cur).strip())
    return pieces


async def chunk_exam_markdown(
    markdown_text: str,
    *,
    max_chars: int = 3000,
    overlap: int = 150,
) -> list[dict]:
    """Chunk exam markdown by question boundary (Câu/Bài/Question), keeping each
    question whole and never splitting inside a ``$$...$$`` / ``$...$`` formula.

    Falls back to :func:`chunk_markdown_text` when no question markers are found
    (non-exam documents). Same chunk dict shape as ``chunk_document``.
    """
    if not markdown_text or len(markdown_text.strip()) < 30:
        return []

    # Doc title = first heading line (context prefix for embeddings).
    doc_title = ""
    for line in markdown_text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            doc_title = s.lstrip("#").strip()
            break

    masked, masks = _mask_formulas(markdown_text)

    # Question boundaries (reuse the parser's markers; lazy import avoids cycle).
    try:
        from app.services.pipeline import _find_question_markers
        markers = _find_question_markers(masked)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("chunk_exam_markdown: marker import failed (%s) → fallback", exc)
        markers = []

    if not markers:
        return await chunk_markdown_text(markdown_text, max_chunk_chars=2000)

    bounds = [m.start() for m in markers]
    segments: list[tuple[int, int]] = []
    if bounds[0] > 0:
        segments.append((0, bounds[0]))  # preamble (đề header) trước Câu 1
    for i, st in enumerate(bounds):
        en = bounds[i + 1] if i + 1 < len(bounds) else len(masked)
        segments.append((st, en))

    chunks: list[dict] = []
    for st, en in segments:
        seg_masked = masked[st:en].strip()
        if not seg_masked:
            continue
        first_line = seg_masked.split("\n", 1)[0].strip().lstrip("#").strip()
        title = _unmask_formulas(first_line, masks)[:80]
        heading_trail = " > ".join(p for p in [doc_title, title] if p)
        for piece_masked in _split_oversized(seg_masked, max_chars, overlap):
            piece = _unmask_formulas(piece_masked, masks).strip()
            if len(piece) < 30:
                continue
            chunks.append({
                "text": piece,
                "index": len(chunks),
                "section_title": title,
                "page": None,
                "heading_trail": heading_trail,
                "char_count": len(piece),
            })

    logger.info("Exam chunker: %d chunks from %d chars (%d questions)", len(chunks), len(markdown_text), len(bounds))
    return chunks
