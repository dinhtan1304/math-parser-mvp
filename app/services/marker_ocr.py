"""Marker OCR — Best-in-class document → Markdown conversion.

Uses Surya engine under the hood for:
- Layout detection
- OCR with LaTeX math recognition
- Table structure detection
- Reading order resolution

Output: Clean Markdown ready for AI parsing.

Install: pip install marker-pdf
First run downloads ~2-3GB of ML models.
"""

import os
import time
import asyncio
import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MAX_WORKERS = int(os.getenv("MARKER_MAX_WORKERS", "0")) or max(2, min(4, (os.cpu_count() or 4) // 2))
_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS)
_converters = {}  # Lazy-loaded by config
_models_cache = None  # Sprint 7: model artifact_dict singleton (load 1 lần ~2-3GB)
_available: Optional[bool] = None

# Adaptive timeout: base 90s + 45s/page, capped at 1500s.
# Sprint 7.1: bump default — Surya texify model ~30-60s/page cho math-heavy doc
# (đề HSG toán nhiều fraction/integral). Cũ 12s/page chỉ đủ cho text thuần.
_TIMEOUT_BASE_SECONDS = int(os.getenv("MARKER_TIMEOUT_BASE", "90"))
_TIMEOUT_PER_PAGE_SECONDS = int(os.getenv("MARKER_TIMEOUT_PER_PAGE", "45"))
_TIMEOUT_CAP_SECONDS = int(os.getenv("MARKER_TIMEOUT_CAP", "1500"))
# Sub-retry: split PDF into chunks of this many pages on timeout/failure
_SUB_RETRY_PAGES = int(os.getenv("MARKER_SUB_RETRY_PAGES", "5"))

# Math-tuned options for STEM subjects.
# - equation_model_max_length: 1024 (default) → 2048 — long math expressions
#   (đặc biệt cho hệ phương trình, định thức, tổ hợp) cần token budget cao hơn,
#   nếu không Surya texify sẽ truncate giữa biểu thức.
# - equation_batch_size: tăng parallelism khi nhiều equation/page.
# - inline_math_delimiters: force $...$ (LaTeX standard) để Gemini parse chuẩn.
_EQUATION_MODEL_MAX_LEN = int(os.getenv("MARKER_EQUATION_MAX_LEN", "2048"))
_EQUATION_BATCH_SIZE = int(os.getenv("MARKER_EQUATION_BATCH", "8"))
_INLINE_MATH_DELIMS = os.getenv("MARKER_INLINE_MATH_DELIMS", "$,$")  # comma-separated open,close


def _estimate_page_count(file_path: str) -> int:
    """Best-effort page count cho timeout adaptive. Fallback = 1 nếu fail."""
    try:
        ext = os.path.splitext(file_path)[1].lower()
        if ext != ".pdf":
            return 1
        import fitz  # type: ignore

        doc = fitz.open(file_path)
        try:
            return max(1, len(doc))
        finally:
            doc.close()
    except Exception:
        return 1


def _compute_timeout(page_count: int) -> int:
    return min(
        _TIMEOUT_CAP_SECONDS,
        _TIMEOUT_BASE_SECONDS + max(1, page_count) * _TIMEOUT_PER_PAGE_SECONDS,
    )


def is_available() -> bool:
    """Check if marker-pdf is installed."""
    global _available
    if _available is None:
        try:
            import marker  # noqa: F401
            _available = True
            logger.info("marker-pdf available")
        except ImportError:
            _available = False
            logger.info("marker-pdf not installed — Marker OCR disabled")
    return _available


def _build_math_config(force_ocr: bool) -> dict:
    """Math-tuned Marker config. Áp dụng cho cả default và force_ocr converter:
    đảm bảo công thức/dạng toán được extract đúng dưới dạng LaTeX $...$.
    """
    delims = _INLINE_MATH_DELIMS.split(",")
    open_d = delims[0] if delims else "$"
    close_d = delims[1] if len(delims) > 1 else open_d
    cfg: dict = {
        "output_format": "markdown",
        # Đảm bảo inline math wrap $...$ — Gemini system prompt yêu cầu cú pháp này.
        # Default trong Marker đã là ('$', '$') nhưng set explicit để stability.
        "inline_math_delimiters": [open_d, close_d],
        # Surya texify model: tăng max_length cho biểu thức dài (hệ pt, định thức,
        # ma trận lớn). Default 1024 → 2048 — giảm truncation giữa expression.
        "model_max_length": _EQUATION_MODEL_MAX_LEN,
        # Batch để parallel xử lý equations (default None = ~4)
        "equation_batch_size": _EQUATION_BATCH_SIZE,
        # Skip image extraction để giảm 10-20% thời gian Marker. LaTeX vẫn được
        # Surya texify; chỉ ảnh figure bị bỏ qua. Tắt env nếu cần overlay.
        "disable_image_extraction": os.getenv(
            "MARKER_DISABLE_IMAGE_EXTRACTION", "1"
        ).strip().lower() not in {"0", "false", "no", "off"},
    }
    if force_ocr:
        cfg["force_ocr"] = True
    return cfg


def _html_to_text(html: str | None) -> str:
    """Best-effort visible text extraction from Marker JSON block HTML."""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for math_node in soup.find_all("math"):
            latex = math_node.get_text(" ", strip=True)
            math_node.replace_with(f"${latex}$" if latex else "")
        return soup.get_text(" ", strip=True)
    except Exception:
        return str(html).strip()


def _normalize_marker_bbox(
    bbox: list[float] | tuple[float, float, float, float] | None,
    page_bbox: list[float] | tuple[float, float, float, float] | None,
) -> list[float] | None:
    if not bbox or len(bbox) != 4 or not page_bbox or len(page_bbox) != 4:
        return None
    page_x0, page_y0, page_x1, page_y1 = [float(v) for v in page_bbox]
    width = page_x1 - page_x0
    height = page_y1 - page_y0
    if width <= 0 or height <= 0:
        return None
    x0, y0, x1, y1 = [float(v) for v in bbox]
    return [
        round(max(0.0, min(1.0, (x0 - page_x0) / width)), 6),
        round(max(0.0, min(1.0, (y0 - page_y0) / height)), 6),
        round(max(0.0, min(1.0, (x1 - page_x0) / width)), 6),
        round(max(0.0, min(1.0, (y1 - page_y0) / height)), 6),
    ]


def _marker_kind(block_type: str) -> str:
    normalized = (block_type or "").lower()
    if normalized == "sectionheader":
        return "heading"
    if normalized == "equation":
        return "formula"
    if normalized in {"table", "tablegroup"}:
        return "table"
    if normalized in {"picture", "picturegroup", "figure", "figuregroup"}:
        return "figure"
    return "paragraph"


def _flatten_marker_json_layout(
    rendered_json: Any,
    *,
    page_offset: int = 0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert Marker JSONRenderer output into normalized blocks + figure hints.

    Marker page ids are zero-based and each block bbox is expressed in page-space
    coordinates.  The review pipeline uses one-based page numbers + normalized
    coordinates, so we normalize here once and keep that contract downstream.
    """
    pages = list(getattr(rendered_json, "children", []) or [])
    blocks: list[dict[str, Any]] = []
    figures: list[dict[str, Any]] = []
    allowed_leaf_types = {
        "Text",
        "SectionHeader",
        "Equation",
        "Table",
        "Caption",
        "ListItem",
        "Code",
        "Form",
        "Handwriting",
        "Picture",
        "Figure",
    }
    image_types = {"Picture", "Figure"}

    for local_page_idx, page in enumerate(pages, start=1):
        page_num = local_page_idx + page_offset
        page_bbox = list(getattr(page, "bbox", []) or [])
        page_counter = 0

        def visit(node: Any) -> None:
            nonlocal page_counter
            block_type = str(getattr(node, "block_type", "") or "")
            children = list(getattr(node, "children", []) or [])
            if block_type in allowed_leaf_types:
                page_counter += 1
                bbox = _normalize_marker_bbox(
                    list(getattr(node, "bbox", []) or []),
                    page_bbox,
                )
                text = _html_to_text(getattr(node, "html", ""))
                if block_type in image_types and not text:
                    text = "[FIGURE]"
                if text or block_type in image_types:
                    block_id = f"p{page_num}_marker_{page_counter}"
                    blocks.append(
                        {
                            "block_id": block_id,
                            "page_num": page_num,
                            "order": len(blocks),
                            "text": text,
                            "source": "marker",
                            "bbox": bbox,
                            "kind": _marker_kind(block_type),
                            "marker_block_id": str(getattr(node, "id", "")),
                            "marker_block_type": block_type,
                        }
                    )
                    if block_type in image_types:
                        try:
                            from marker.settings import settings

                            image_ext = settings.OUTPUT_IMAGE_FORMAT.lower()
                        except Exception:
                            image_ext = "jpeg"
                        marker_id = str(getattr(node, "id", "") or "")
                        placeholder = (
                            f"{marker_id.replace('/', '_')}.{image_ext}"
                            if marker_id
                            else None
                        )
                        figures.append(
                            {
                                "figure_id": f"p{page_num}_marker_fig_{len(figures) + 1}",
                                "page_num": page_num,
                                "bbox": bbox,
                                "placeholder": placeholder,
                                "source": "marker_json",
                                "marker_block_id": marker_id,
                            }
                        )
            for child in children:
                visit(child)

        for child in list(getattr(page, "children", []) or []):
            visit(child)

    return blocks, figures


def _get_converter(force_ocr: bool = False):
    """Sprint 7 — Lazy-load Marker converter dùng canonical API.

    Marker `>=1.0.0` yêu cầu `PdfConverter(artifact_dict, processor_list, renderer,
    llm_service, config=...)` — KHÔNG chỉ `PdfConverter(config=config)` (bug cũ).

    Models được load 1 lần per process (`_models_cache` singleton, ~2-3GB,
    ~30-60s lần đầu nếu chưa cache disk).
    """
    global _converters, _models_cache
    key = "force_ocr" if force_ocr else "default"
    if key in _converters:
        return _converters[key]

    from marker.converters.pdf import PdfConverter
    from marker.config.parser import ConfigParser
    from marker.models import create_model_dict

    if _models_cache is None:
        logger.info("Loading Marker models (first time, ~2-3GB)...")
        t0 = time.time()
        try:
            _models_cache = create_model_dict()
        except Exception as e:
            logger.error("Marker create_model_dict failed: %s", e, exc_info=True)
            raise
        logger.info("Marker models loaded in %.1fs", time.time() - t0)

    config_values = _build_math_config(force_ocr=force_ocr)
    cp = ConfigParser(config_values)
    try:
        _converters[key] = PdfConverter(
            config=cp.generate_config_dict(),
            artifact_dict=_models_cache,
            processor_list=cp.get_processors(),
            renderer=cp.get_renderer(),
            llm_service=cp.get_llm_service(),
        )
        logger.info(
            "Marker converter loaded (%s) — math-tuned: max_len=%d, batch=%d, delims=%s",
            key, _EQUATION_MODEL_MAX_LEN, _EQUATION_BATCH_SIZE,
            config_values["inline_math_delimiters"],
        )
    except Exception as e:
        logger.error("Marker converter init failed: %s", e, exc_info=True)
        raise
    return _converters[key]


async def extract_markdown(
    file_path: str,
    use_llm: bool = False,
    force_ocr: bool = False,
    timeout: Optional[int] = None,
) -> dict:
    """Convert document to clean Markdown using Marker.

    Args:
        file_path: Path to PDF/DOCX/PPTX/HTML file.
        use_llm: If True, use LLM to boost accuracy for complex tables/math.
        force_ocr: Force Marker re-OCR ngay cả với text-based PDF.
        timeout: Override timeout (s). Default = adaptive theo page count.

    Returns:
        {
            "text": str (clean markdown),
            "page_count": int,
            "method": "marker",
            "image_map": dict,
        }
    """
    if not is_available():
        logger.warning("Marker not installed, returning empty result")
        return {
            "text": "",
            "page_count": 0,
            "method": "marker-not-installed",
            "image_map": {},
        }

    loop = asyncio.get_running_loop()
    effective_timeout = timeout if timeout is not None else _compute_timeout(_estimate_page_count(file_path))

    def _convert():
        converter = _get_converter(force_ocr=force_ocr)
        from marker.renderers.json import JSONRenderer

        document = converter.build_document(file_path)
        converter.page_count = len(document.pages)
        markdown_renderer = converter.resolve_dependencies(converter.renderer)
        rendered = markdown_renderer(document)
        json_renderer = converter.resolve_dependencies(JSONRenderer)
        rendered_json = json_renderer(document)

        # Extract markdown text
        markdown = ""
        if hasattr(rendered, "markdown"):
            markdown = rendered.markdown
        elif hasattr(rendered, "text"):
            markdown = rendered.text

        # Count pages from the shared Marker document so markdown/json stay aligned.
        page_count = len(document.pages)

        # Extract images if available
        image_map = {}
        if hasattr(rendered, "images") and rendered.images:
            for img_name, img_data in rendered.images.items():
                image_map[img_name] = img_data

        blocks, figures = _flatten_marker_json_layout(rendered_json)

        return {
            "text": markdown,
            "page_count": page_count,
            "method": "marker-force-ocr" if force_ocr else "marker",
            "image_map": image_map,
            "blocks": blocks,
            "figures": figures,
            "layout_source": "marker_json",
        }

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(_executor, _convert),
            timeout=effective_timeout,
        )
        text_len = len(result.get("text", ""))
        pages = result.get("page_count", 0)
        logger.info(
            "Marker OCR complete: %d chars, %d pages (timeout=%ds)",
            text_len, pages, effective_timeout,
        )
        return result
    except asyncio.TimeoutError:
        logger.warning(
            "Marker OCR timed out after %ds — caller may try sub-retry per-page",
            effective_timeout,
        )
        return {
            "text": "",
            "page_count": 0,
            "method": "marker-timeout",
            "image_map": {},
            "timeout_seconds": effective_timeout,
        }
    except Exception as e:
        logger.warning(f"Marker OCR failed: {e}")
        return {
            "text": "",
            "page_count": 0,
            "method": "marker-error",
            "image_map": {},
        }


async def smoke_test() -> dict:
    """Sprint 7 S7.2 — verify Marker thực sự work end-to-end.

    Tạo 1 PDF mini có công thức đơn giản, chạy Marker, kiểm tra output có text +
    LaTeX-like markers. Trả về dict diagnostic để dùng ở /healthz hoặc startup hook.
    """
    if not is_available():
        return {"ok": False, "error": "marker not installed", "latex_detected": False}

    try:
        import fitz  # type: ignore
    except Exception as exc:
        return {"ok": False, "error": f"PyMuPDF not available: {exc}", "latex_detected": False}

    tmp_path: Optional[str] = None
    try:
        # Tạo PDF mini với text math đơn giản — Marker phải extract được
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((50, 100), "Test: x^2 + 1/2 = 0", fontsize=14)
        page.insert_text((50, 150), "Equation: a + b = c", fontsize=14)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            tmp_path = f.name
        doc.save(tmp_path)
        doc.close()

        result = await extract_markdown(tmp_path, force_ocr=False, timeout=180)
        text = (result.get("text") or "").strip()
        method = result.get("method", "unknown")
        blocks = result.get("blocks") or []
        bbox_blocks = [block for block in blocks if block.get("bbox")]
        # Marker xuất ra `$...$` hoặc `\frac` cho công thức; với text đơn giản
        # có thể không có LaTeX delimiters nhưng phải có text content.
        # NOTE: `x^` là plain ASCII, KHÔNG phải LaTeX → bỏ khỏi check.
        latex_detected = any(t in text for t in ["$", "\\frac", "\\sqrt"])
        return {
            "ok": bool(text),
            "error": None if text else f"empty output, method={method}",
            "latex_detected": latex_detected,
            "method": method,
            "text_preview": text[:200],
            "layout_blocks": len(blocks),
            "bbox_blocks": len(bbox_blocks),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"smoke_test crashed: {exc}",
            "latex_detected": False,
        }
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


async def extract_markdown_with_subretry(
    file_path: str,
    force_ocr: bool = False,
    pages_per_chunk: Optional[int] = None,
) -> dict:
    """Marker OCR với sub-retry per-page khi full-file timeout/fail.

    Strategy:
      1. Nếu file > 2× chunk size → SKIP full-file (tốn 2× compute), đi thẳng chunked.
      2. Else: thử full file một lần với adaptive timeout.
      3. Nếu fail (timeout / empty / error), split PDF thành chunks
         `pages_per_chunk` trang và retry từng chunk độc lập.
      4. Trang nào fail vẫn không pass được → ghi vào `failed_pages`,
         KHÔNG silently drop.

    Chỉ hỗ trợ PDF — DOCX/PPTX không thể split per-page.
    """
    if not is_available():
        return {"text": "", "page_count": 0, "method": "marker-not-installed", "image_map": {}, "failed_pages": []}

    ext = os.path.splitext(file_path)[1].lower()
    if ext != ".pdf":
        # Non-PDF: chỉ thử full file, không sub-retry
        result = await extract_markdown(file_path, force_ocr=force_ocr)
        result.setdefault("failed_pages", [])
        return result

    page_count = _estimate_page_count(file_path)
    chunk_size = pages_per_chunk or _SUB_RETRY_PAGES
    full_result = {
        "text": "",
        "page_count": page_count,
        "method": "marker-not-attempted",
        "image_map": {},
        "blocks": [],
        "figures": [],
    }

    # Sprint 7 hotfix: cho file > chunk_size pages, SKIP full-file attempt vì
    # CPU run gần như luôn bị cap. Sub-retry chunked + parallel cho output tương
    # đương với chi phí thấp hơn nhiều.
    skip_full = page_count > chunk_size
    if skip_full:
        logger.info(
            "Marker: %d pages > %d → SKIP full-file, dùng chunked ngay",
            page_count, chunk_size,
        )
    else:
        # 1. Thử full file
        full_result = await extract_markdown(file_path, force_ocr=force_ocr)
        full_text = full_result.get("text", "") or ""
        full_method = full_result.get("method", "")
        if full_text.strip() and full_method not in ("marker-timeout", "marker-error"):
            full_result.setdefault("failed_pages", [])
            return full_result
        logger.warning(
            "Marker full-file failed (method=%s, len=%d) → starting per-chunk sub-retry",
            full_method, len(full_text),
        )

    # 2. Split + retry (chunked path)
    chunks_text: list[str] = []
    merged_blocks: list[dict[str, Any]] = []
    merged_figures: list[dict[str, Any]] = []
    merged_image_map: dict[str, Any] = {}
    failed_pages: list[int] = []
    used_method = "marker-chunked-force-ocr" if force_ocr else "marker-chunked"

    try:
        import fitz  # type: ignore
    except Exception as exc:
        logger.warning("PyMuPDF not available, cannot sub-retry: %s", exc)
        full_result.setdefault("failed_pages", list(range(1, page_count + 1)))
        return full_result

    # Step 1: split PDF into temp chunks (sync, fast — disk I/O only)
    chunk_specs: list[tuple[str, list[int], int]] = []  # (tmp_path, chunk_pages, start_offset)
    try:
        src = fitz.open(file_path)
        try:
            for start in range(0, page_count, chunk_size):
                end = min(start + chunk_size, page_count)
                chunk_pages = list(range(start + 1, end + 1))
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp_path = tmp.name
                sub = fitz.open()
                sub.insert_pdf(src, from_page=start, to_page=end - 1)
                sub.save(tmp_path)
                sub.close()
                chunk_specs.append((tmp_path, chunk_pages, start))
        finally:
            src.close()
    except Exception as exc:
        logger.warning("Marker chunk split failed: %s", exc)
        full_result.setdefault("failed_pages", list(range(1, page_count + 1)))
        return full_result

    # Step 2: run chunks sequentially. PdfConverter singleton is NOT thread-safe;
    # asyncio.gather caused race conditions corrupting all chunks. Sequential
    # still benefits from skip-full optimization and smaller chunk sizes.
    sub_results: list = []
    for spec in chunk_specs:
        try:
            sub_results.append(await extract_markdown(spec[0], force_ocr=force_ocr))
        except BaseException as exc:
            sub_results.append(exc)

    # Step 3: merge in order + cleanup temp files
    for (tmp_path, chunk_pages, chunk_offset), sub_result in zip(chunk_specs, sub_results):
        try:
            if isinstance(sub_result, BaseException):
                failed_pages.extend(chunk_pages)
                logger.warning(
                    "Sub-retry pages %d-%d crashed: %s",
                    chunk_pages[0], chunk_pages[-1], sub_result,
                )
                continue
            sub_text = sub_result.get("text", "") or ""
            if sub_text.strip():
                chunks_text.append(sub_text)
                for block in sub_result.get("blocks", []) or []:
                    adjusted = dict(block)
                    adjusted["page_num"] = int(block.get("page_num") or 1) + chunk_offset
                    adjusted["block_id"] = (
                        f"p{adjusted['page_num']}_marker_{len(merged_blocks) + 1}"
                    )
                    adjusted["order"] = len(merged_blocks)
                    merged_blocks.append(adjusted)
                for figure in sub_result.get("figures", []) or []:
                    adjusted = dict(figure)
                    adjusted["page_num"] = int(figure.get("page_num") or 1) + chunk_offset
                    adjusted["figure_id"] = (
                        f"p{adjusted['page_num']}_marker_fig_{len(merged_figures) + 1}"
                    )
                    merged_figures.append(adjusted)
                merged_image_map.update(sub_result.get("image_map", {}) or {})
                logger.info(
                    "Sub-retry pages %d-%d: %d chars",
                    chunk_pages[0], chunk_pages[-1], len(sub_text),
                )
            else:
                failed_pages.extend(chunk_pages)
                logger.warning(
                    "Sub-retry pages %d-%d: still empty (method=%s)",
                    chunk_pages[0], chunk_pages[-1], sub_result.get("method"),
                )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    merged_text = "\n\n".join(chunks_text)
    logger.info(
        "Marker sub-retry done: %d/%d pages OK, %d pages failed",
        page_count - len(failed_pages), page_count, len(failed_pages),
    )
    return {
        "text": merged_text,
        "page_count": page_count,
        "method": used_method,
        "image_map": merged_image_map,
        "blocks": merged_blocks,
        "figures": merged_figures,
        "layout_source": "marker_json",
        "failed_pages": failed_pages,
    }


async def extract_markdown_with_fallback(file_path: str) -> dict:
    """Try Marker first, fallback to PyMuPDF if Marker fails or unavailable.

    This is the safe entry point for the pipeline — never raises.
    """
    # Try Marker
    result = await extract_markdown(file_path)
    if result.get("text"):
        return result

    # Fallback to PyMuPDF
    logger.info("Marker failed/empty, falling back to PyMuPDF")
    try:
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".pdf":
            import fitz

            doc = fitz.open(file_path)
            text_parts = []
            for page_num in range(len(doc)):
                page = doc[page_num]
                text = page.get_text("text")
                if text.strip():
                    text_parts.append(f"[Trang {page_num + 1}]\n{text}")
            page_count = len(doc)
            doc.close()

            return {
                "text": "\n\n".join(text_parts),
                "page_count": page_count,
                "method": "pymupdf-fallback",
                "image_map": {},
            }
    except Exception as e:
        logger.warning(f"PyMuPDF fallback also failed: {e}")

    return {
        "text": "",
        "page_count": 0,
        "method": "all-failed",
        "image_map": {},
    }
