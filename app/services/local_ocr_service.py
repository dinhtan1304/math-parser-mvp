"""Local OCR artifact service.

Core upload parsing must not send images to Gemini. This service runs local OCR,
persists a small artifact by file hash, and reports quality/stats to callers.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ARTIFACT_DIR = Path("uploads") / "ocr_artifacts"
TEXT_HEAVY_SUBJECTS = {"ngu-van", "tieng-anh", "lich-su", "gdcd", "gdktpl", "tin-hoc"}
PDF_MARKER_SUBJECTS = {"toan", "vat-li", "hoa-hoc", "ielts"}
# STEM subjects yêu cầu công thức LaTeX. Nếu OCR không trả LaTeX markers
# → high chance math bị OCR sai → cần re-extract.
STEM_SUBJECTS = {"toan", "vat-li", "hoa-hoc", "sinh-hoc", "khtn", "khoa-hoc"}
NATIVE_MATH_FULL_MARKER_RESCUE = os.getenv("OCR_NATIVE_MATH_FULL_MARKER_RESCUE", "0").strip().lower() in {"1", "true", "yes", "on"}
LATEX_MARKER_RESCUE_MAX_PAGES = int(os.getenv("OCR_LATEX_MARKER_RESCUE_MAX_PAGES", "3").split("#", 1)[0].strip() or "3")

# A4 — Empty extraction guard threshold: dưới ngưỡng này coi như chưa OCR được
EMPTY_TEXT_THRESHOLD = int(os.getenv("OCR_EMPTY_TEXT_THRESHOLD", "100"))

# Cascade engine (chế độ engine="auto"). PaddleOCR-VL-1.6 là OCR chính (self-host,
# $0, SOTA OmniDocBench v1.6, giữ LaTeX + tiếng Việt); MinerU làm fallback/baseline
# so sánh. Khi primary lỗi/empty/low-quality (hoặc STEM math bị làm phẳng) → chạy
# fallback (quality-gate qua _fallback_beats_primary). Đặt OCR_PRIMARY_ENGINE=mineru
# để quay lại hành vi cũ mà không sửa code.
OCR_PRIMARY_ENGINE = os.getenv("OCR_PRIMARY_ENGINE", "paddle-vl").strip().lower()  # paddle-vl | mineru
# PaddleOCR-VL chạy ở đâu: local (in-process GPU) | api (HTTP service hosted). Mode
# api gọi remote → KHÔNG cần paddle/torch/GPU local (xem app/services/paddle_vl_api.py).
PADDLE_VL_MODE = os.getenv("PADDLE_VL_MODE", "local").strip().lower()  # local | api
OCR_FALLBACK_ENABLED = os.getenv("OCR_FALLBACK_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
# Mặc định fallback = engine còn lại so với primary; cho phép override tường minh.
OCR_FALLBACK_ENGINE = os.getenv(
    "OCR_FALLBACK_ENGINE",
    "mineru" if OCR_PRIMARY_ENGINE == "paddle-vl" else "paddle-vl",
).strip().lower()

# A5 — Cache schema version: bump khi sửa logic OCR cascade hoặc parser
# để invalidate artifact cũ. Đổi mỗi khi thay đổi chuỗi extract/escalation.
# v3 = math-tuned Marker + LaTeX quality validator (2026-05-15)
# v4 = tofu/PUA char detection + post-process Gemini output (2026-05-16)
# v5 = Sprint 6: splitter regex fix + answer key separation (2026-05-16)
# v6 = Sprint 7: Marker init bug fix — Marker FINALLY runs cho STEM doc (2026-05-16)
# v7 = Formula fidelity pilot: native-text math corruption audit + publishability flags (2026-05-17)
# v15 = PaddleOCR-VL fallback cascade + publishable respects is_empty + content_list validation (2026-06-05)
# v16 = PaddleOCR-VL-1.6 primary + MinerU fallback (cascade đảo chiều) + Paddle layout bbox blocks (2026-06-08)
OCR_CACHE_SCHEMA_VERSION = "v16"

# B2 — Concurrency guard. MinerU/PaddleOCR-VL load 2-3GB models; parallel uploads
# would multiply memory → OOM. Serialize heavy OCR runs across the process via a
# semaphore (default 1; raise on big-RAM/multi-GPU hosts via OCR_MAX_CONCURRENCY).
OCR_MAX_CONCURRENCY = max(1, int(os.getenv("OCR_MAX_CONCURRENCY", "1").split("#", 1)[0].strip() or "1"))
_ocr_semaphore: asyncio.Semaphore | None = None


def _get_ocr_semaphore() -> asyncio.Semaphore:
    # Lazy init so the semaphore binds to the active event loop.
    global _ocr_semaphore
    if _ocr_semaphore is None:
        _ocr_semaphore = asyncio.Semaphore(OCR_MAX_CONCURRENCY)
    return _ocr_semaphore


def _ocr_limited(fn):
    """Decorator: run a heavy OCR engine coroutine under the global concurrency cap.

    Cancellation-safe: if the caller times out (``asyncio.wait_for``) while the
    engine thread/subprocess is still running, the semaphore is NOT released
    until that work actually finishes — otherwise the next upload would start a
    second 2-3GB engine concurrently and OOM the host. The inner task is
    shielded (not cancelled) so its own ``finally`` cleanup still runs; the
    engine subprocess self-terminates at its own timeout (see
    ``run_command_template`` process-tree kill), so the deferred release always
    happens.
    """
    @functools.wraps(fn)
    async def _wrapper(*args, **kwargs):
        sem = _get_ocr_semaphore()
        await sem.acquire()
        inner = asyncio.ensure_future(fn(*args, **kwargs))
        release_deferred = False
        try:
            return await asyncio.shield(inner)
        except asyncio.CancelledError:
            if not inner.done():
                release_deferred = True

                def _release_when_done(task: asyncio.Task) -> None:
                    if not task.cancelled() and task.exception() is not None:
                        logger.warning(
                            "OCR engine finished after caller cancelled: %s",
                            task.exception(),
                        )
                    sem.release()

                inner.add_done_callback(_release_when_done)
            raise
        finally:
            if not release_deferred:
                sem.release()
    return _wrapper


# ─── LaTeX/math quality detection ────────────────────────────────────────────

# LaTeX command markers — Marker với math config đúng phải sinh ra các pattern này
_RE_LATEX_INLINE = re.compile(r'\$[^$\n]{1,200}\$')  # $...$ inline math
_RE_LATEX_BLOCK = re.compile(r'\$\$[\s\S]{1,500}?\$\$')  # $$...$$ block math
# Trailing lookahead `(?![a-zA-Z])` thay vì `\b` vì `\b` không match khi
# theo sau là `_` (word char) — case rất phổ biến: `\int_0`, `\sum_{i=1}`.
_RE_LATEX_COMMANDS = re.compile(
    r'\\(?:frac|sqrt|sum|int|prod|lim|cdot|times|div|infty|pi|alpha|beta|gamma|'
    r'theta|lambda|mu|sigma|omega|partial|nabla|leq|geq|neq|approx|equiv|'
    r'rightarrow|leftarrow|Rightarrow|Leftarrow|forall|exists|in|notin|subset|'
    r'cup|cap|begin|end|left|right|widehat|overrightarrow|overline|underline|'
    r'mathbb|mathcal|mathbf|text|sin|cos|tan|cot|log|ln|exp)(?![a-zA-Z])'
)
# Math marker không LaTeX: ký tự Unicode toán, dấu mũ/chỉ số dạng plain text
_RE_PLAIN_MATH_HINT = re.compile(
    r'[²³⁴⁵⁶⁷⁸⁹°±×÷≤≥≠∞∑∫√π∠∆∇∂∈∉⊂⊃∪∩→←↔]|'
    r'(?<![a-zA-Z])(?:sin|cos|tan|cot|log|ln|lim|sqrt)\s*\('
)
_RE_NATIVE_POWER_FLATTEN = re.compile(
    r'(?<![\w])(?:[A-Za-z]\d{1,4}|\([^)\n]{1,40}\)\d{2,4})(?![\w])'
)
_RE_NATIVE_SHORT_MATH_LINE = re.compile(
    r'^\s*(?:'
    r'[A-Za-z]\d{0,4}'
    r'|[A-Za-z]{1,3}'
    r'|\d{1,4}'
    r'|[+\-−=×÷]'
    r'|[A-Za-z]\s*[+\-−=]\s*[A-Za-z0-9]+'
    r')\s*$'
)


def assess_latex_quality(text: str, subject_code: str | None = None) -> dict[str, Any]:
    """Đánh giá chất lượng LaTeX extraction cho math/STEM docs.

    Returns:
        {
            "is_stem": bool,
            "latex_inline_count": int,        # số ``$...$``
            "latex_block_count": int,         # số ``$$...$$``
            "latex_command_count": int,       # số ``\\frac``, ``\\sqrt``, ...
            "plain_math_hint_count": int,     # ký tự toán plain (không LaTeX)
            "score": float (0..1),            # 1.0 = math perfectly LaTeX-ized
            "is_math_broken": bool,           # True = math hint nhiều nhưng LaTeX ít
            "reason": str,
        }
    """
    raw = (text or "")
    is_stem = (subject_code or "").lower() in STEM_SUBJECTS
    inline = len(_RE_LATEX_INLINE.findall(raw))
    block = len(_RE_LATEX_BLOCK.findall(raw))
    cmds = len(_RE_LATEX_COMMANDS.findall(raw))
    plain_hints = len(_RE_PLAIN_MATH_HINT.findall(raw))

    # Tổng tín hiệu math (LaTeX-ized hoặc plain)
    total_math_signal = inline + block + cmds + plain_hints
    latex_ratio = (inline + block + cmds) / max(total_math_signal, 1)

    reasons: list[str] = []
    score = 1.0

    if not is_stem:
        # Non-STEM: bất cứ LaTeX nào cũng là bonus, không penalize
        score = 1.0
        is_broken = False
    else:
        # STEM doc: phải có LaTeX nếu có math signal
        if total_math_signal == 0:
            # Không có math signal nào — có thể doc chỉ có lý thuyết, không bài tập
            score = 0.7
            reasons.append("no_math_signal_in_stem_doc")
            is_broken = False
        elif latex_ratio < 0.3 and plain_hints >= 5:
            # Có math signal nhưng < 30% được LaTeX-ize → broken
            score = 0.3
            reasons.append(f"low_latex_ratio={latex_ratio:.2f}")
            is_broken = True
        elif latex_ratio < 0.6 and plain_hints >= 10:
            score = 0.6
            reasons.append(f"moderate_latex_ratio={latex_ratio:.2f}")
            is_broken = True
        else:
            score = min(1.0, 0.5 + latex_ratio * 0.5)
            is_broken = False

    return {
        "is_stem": is_stem,
        "latex_inline_count": inline,
        "latex_block_count": block,
        "latex_command_count": cmds,
        "plain_math_hint_count": plain_hints,
        "latex_ratio": round(latex_ratio, 3) if total_math_signal else 0.0,
        "score": round(score, 3),
        "is_math_broken": is_broken,
        "reason": ",".join(reasons) or "ok",
    }


def assess_native_math_text(text: str | None, subject_code: str | None = None) -> dict[str, Any]:
    """Detect math semantics likely lost by native PDF text extraction.

    Text-native LaTeX PDFs often look perfect visually while their extracted text
    flattens powers/fractions or inserts opaque control chars.  This audit is
    deliberately conservative: it does not claim that the OCR output is wrong;
    it only records that the native text layer is unsafe to trust for math.
    """
    raw = text or ""
    is_stem = (subject_code or "").lower() in STEM_SUBJECTS
    if not raw.strip():
        return {
            "is_stem": is_stem,
            "is_corrupted": False,
            "score": 0,
            "reason": "empty_or_unavailable",
            "control_char_count": 0,
            "flattened_power_count": 0,
            "stacked_math_pair_count": 0,
            "short_math_line_count": 0,
        }

    lines = raw.splitlines()
    control_char_count = sum(
        1 for c in raw if ord(c) < 32 and c not in "\n\r\t"
    )
    flattened_power_count = len(_RE_NATIVE_POWER_FLATTEN.findall(raw))
    short_math_line_count = sum(
        1 for line in lines if _RE_NATIVE_SHORT_MATH_LINE.match(line or "")
    )

    # Fractions/superscripts often become two adjacent short lines, e.g.
    # ``a2`` then ``bc`` or ``1`` then ``2025``.  Count only compact neighbors
    # to avoid flagging normal prose.
    stacked_math_pair_count = 0
    for left, right in zip(lines, lines[1:]):
        if (
            _RE_NATIVE_SHORT_MATH_LINE.match(left or "")
            and _RE_NATIVE_SHORT_MATH_LINE.match(right or "")
        ):
            stacked_math_pair_count += 1

    reasons: list[str] = []
    score = 0
    if control_char_count >= 2:
        score += 2
        reasons.append(f"control_chars={control_char_count}")
    if flattened_power_count >= 2:
        score += 2
        reasons.append(f"flattened_powers={flattened_power_count}")
    if stacked_math_pair_count >= 3:
        score += 2
        reasons.append(f"stacked_math_pairs={stacked_math_pair_count}")
    if short_math_line_count >= 8:
        score += 1
        reasons.append(f"short_math_lines={short_math_line_count}")

    is_corrupted = bool(is_stem and score >= 3)
    return {
        "is_stem": is_stem,
        "is_corrupted": is_corrupted,
        "score": score,
        "reason": ",".join(reasons) or "ok",
        "control_char_count": control_char_count,
        "flattened_power_count": flattened_power_count,
        "stacked_math_pair_count": stacked_math_pair_count,
        "short_math_line_count": short_math_line_count,
    }


def _read_native_pdf_text(file_path: str) -> str:
    """Best-effort native text extraction used only for diagnostics."""
    try:
        import fitz  # type: ignore

        doc = fitz.open(file_path)
        try:
            return "\n".join(page.get_text("text") for page in doc)
        finally:
            doc.close()
    except Exception as exc:
        logger.debug("Native PDF text audit unavailable for %s: %s", file_path, exc)
        return ""


def build_formula_audit(
    *,
    subject_code: str | None,
    latex_quality: dict[str, Any],
    native_text_math_audit: dict[str, Any],
    ocr_method: str,
) -> dict[str, Any]:
    """Build a conservative document-level formula audit scaffold.

    This is the first vertical slice of the stricter fidelity plan.  It does not
    yet inspect every formula region, so it only auto-blocks when the available
    document-level evidence says math fidelity is still unsafe after OCR.
    """
    is_stem = (subject_code or "").lower() in STEM_SUBJECTS
    reasons: list[str] = []

    if latex_quality.get("is_math_broken"):
        reasons.append("latex_quality_broken")
    if native_text_math_audit.get("is_corrupted"):
        reasons.append("native_text_math_corruption_detected")
    if is_stem and not str(ocr_method or "").startswith("marker"):
        reasons.append(f"non_marker_stem_output={ocr_method or 'unknown'}")

    review_required = bool(
        is_stem
        and (
            latex_quality.get("is_math_broken")
            or (
                native_text_math_audit.get("is_corrupted")
                and not str(ocr_method or "").startswith("marker")
            )
        )
    )

    return {
        "mode": "document_level_pilot",
        "review_required": review_required,
        "native_text_math_corruption": bool(native_text_math_audit.get("is_corrupted")),
        "latex_quality_broken": bool(latex_quality.get("is_math_broken")),
        "reasons": reasons,
    }


def _artifact_path(file_hash: str, subject_code: str | None, engine: str = "auto") -> Path:
    safe_subject = re.sub(r"[^a-zA-Z0-9_-]", "_", subject_code or "generic")
    # Engine vào cache key — chọn engine khác (mineru/paddle-vl/auto) ra artifact khác.
    safe_engine = re.sub(r"[^a-zA-Z0-9_-]", "_", engine or "auto")
    return ARTIFACT_DIR / f"{file_hash}_{safe_subject}_{safe_engine}_{OCR_CACHE_SCHEMA_VERSION}.json"


async def compute_file_hash(file_path: str) -> str:
    loop = asyncio.get_running_loop()

    def _hash() -> str:
        h = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    return await loop.run_in_executor(None, _hash)


def _count_tofu_chars(text: str) -> int:
    """Đếm 'tofu' / replacement / PUA characters (PyMuPDF extract math glyph
    không có Unicode mapping → cho ra block chars hoặc PUA chars).

    Bao gồm:
      - U+FFFD REPLACEMENT CHARACTER
      - U+258C-U+259F BLOCK ELEMENTS (▌▍▎▏▐░▒▓▔▕▖▗▘▙▚▛▜▝▞▟)
      - PUA Unicode (U+E000-U+F8FF) — Adobe glyph fallback
      - Unmapped CID-style (\\uFFFC OBJECT REPLACEMENT)
    """
    count = 0
    for c in text:
        cp = ord(c)
        if cp == 0xFFFD or cp == 0xFFFC:
            count += 1
        elif 0x2580 <= cp <= 0x259F:  # block elements (▌ etc.)
            count += 1
        elif 0xE000 <= cp <= 0xF8FF:  # PUA
            count += 1
    return count


def assess_ocr_quality(text: str | None, subject_code: str | None = None) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {"score": 0.0, "reason": "empty_text", "is_low_quality": True}

    char_count = len(raw)
    alnum_ratio = sum(1 for c in raw if c.isalnum()) / max(char_count, 1)
    control_ratio = sum(1 for c in raw if ord(c) < 32 and c not in "\n\r\t") / max(char_count, 1)
    tofu_count = _count_tofu_chars(raw)
    tofu_ratio = tofu_count / max(char_count, 1)
    words = re.findall(r"[\wÀ-ỹA-Za-z]{2,}", raw, re.UNICODE)
    unique_words = {w.lower() for w in words}

    score = 1.0
    reasons: list[str] = []
    if char_count < 200:
        score -= 0.45
        reasons.append("too_short")
    if alnum_ratio < 0.45:
        score -= 0.25
        reasons.append("low_alnum_ratio")
    if control_ratio > 0.02:
        score -= 0.25
        reasons.append("control_chars")
    # Tofu chars (▌ ▐ ░ U+FFFD PUA) — dấu hiệu PyMuPDF fail extract math glyph.
    # Ngay cả 1% tofu cũng cần escalate Marker vì math thường tập trung ở
    # vài câu nên ratio thấp nhưng impact lớn.
    if tofu_count >= 5 or tofu_ratio > 0.005:
        score -= 0.4
        reasons.append(f"tofu_chars={tofu_count}")
    if len(unique_words) < 40 and subject_code in TEXT_HEAVY_SUBJECTS | {"ielts"}:
        score -= 0.2
        reasons.append("few_unique_words")

    if subject_code == "ielts":
        lower = raw.lower()
        markers = ["questions", "reading passage", "section", "listening", "writing task", "choose", "true", "false"]
        if sum(1 for marker in markers if marker in lower) < 2:
            score -= 0.25
            reasons.append("missing_ielts_markers")
    else:
        markers = ["Câu", "Bài", "câu", "bài", "Question", "Đề", "=", "+", "Read", "Choose"]
        if sum(1 for marker in markers if marker in raw) < 2:
            score -= 0.15
            reasons.append("missing_question_markers")

    score = max(0.0, min(1.0, score))
    return {
        "score": round(score, 3),
        "reason": ",".join(reasons) or "ok",
        "is_low_quality": score < 0.55,
        "tofu_chars": tofu_count,
    }


def _paddle_vl_available() -> bool:
    """Cheap check whether PaddleOCR-VL can run.

    ``api`` mode → available iff the remote API is configured (no local paddle
    needed). ``local`` mode → verify the `paddleocr` package is importable.
    Never initializes the heavy VL pipeline.
    """
    if PADDLE_VL_MODE == "api":
        try:
            from app.services.paddle_vl_api import is_api_configured

            return is_api_configured()
        except Exception:
            return False
    try:
        from app.benchmark.engines.base import package_available

        return bool(package_available("paddleocr"))
    except Exception:
        return False


def _safe_relocate_image(img_path: str, work_dir: "Path", file_hash: str) -> str | None:
    """Copy ONE OCR-extracted image into ``media_uploads/ocr_images/<hash>/``.

    Returns the public ``/media/...`` URL, or None if the source is missing or
    resolves OUTSIDE ``work_dir`` (path-traversal guard — e.g. an OCR engine
    returning ``img_path='../../../etc/passwd'`` must never be copied/served).
    """
    import shutil as _shutil
    from pathlib import Path as _Path

    if not img_path:
        return None
    p = _Path(img_path)
    candidate = (work_dir / p) if not p.is_absolute() else p
    try:
        work_resolved = work_dir.resolve()
        resolved = candidate.resolve()
        inside = resolved == work_resolved or work_resolved in resolved.parents
    except Exception:
        resolved, inside = candidate, False
    if not inside or not resolved.exists():
        # MinerU sometimes emits a bare filename — locate it *inside* work_dir only.
        matches = list(work_dir.rglob(p.name)) if p.name else []
        if not matches:
            if not inside:
                logger.warning("skip image outside work_dir (possible traversal): %s", img_path)
            return None
        resolved = matches[0].resolve()
    media_root = _Path("media_uploads") / "ocr_images" / file_hash
    media_root.mkdir(parents=True, exist_ok=True)
    dst = media_root / resolved.name
    if not dst.exists():
        try:
            _shutil.copy2(resolved, dst)
        except Exception as exc:
            logger.warning("copy image failed %s -> %s: %s", resolved, dst, exc)
            return None
    return f"/media/ocr_images/{file_hash}/{resolved.name}"


def _relocate_markdown_images(markdown: str, work_dir: "Path", file_hash: str):
    """Scan markdown image refs, relocate each, rewrite via a map.

    Map-based replacement (not a global ``str.replace`` per ref) avoids
    collisions when two refs share a filename. Returns
    ``(markdown, image_map, figures)`` in the production artifact shape.
    Used by the PaddleOCR-VL fallback, whose figures live as markdown links.
    """
    from pathlib import Path as _Path

    image_map: dict[str, str] = {}
    figures: list[dict[str, Any]] = []
    if not markdown:
        return markdown or "", image_map, figures
    url_map: dict[str, str] = {}
    for ref in re.findall(r'!\[[^\]]*\]\(\s*([^)\s]+)', markdown):
        ref = ref.strip()
        if not ref or ref in url_map or ref.startswith(("http://", "https://", "/media/")):
            continue
        new_url = _safe_relocate_image(ref, work_dir, file_hash)
        if not new_url:
            continue
        url_map[ref] = new_url
        image_map[new_url] = new_url
        figures.append({
            "figure_id": f"{OCR_FALLBACK_ENGINE}_fig_{len(figures) + 1}",
            "page_num": None,
            "bbox": None,
            "placeholder": _Path(new_url).name,
            "url": new_url,
            "source": OCR_FALLBACK_ENGINE,
            "caption": None,
        })
    for old, new in url_map.items():
        markdown = markdown.replace(old, new)
    return markdown, image_map, figures


@_ocr_limited
async def _run_mineru_pipeline(
    file_path: str, file_hash: str, budget_seconds: float | None = None
) -> dict[str, Any]:
    """Run MinerUEngine + parse ``content_list.json`` into review-friendly blocks.

    Output shape matches what ``extract_local_ocr_artifact`` writes into the
    cached artifact: ``text``, ``page_count``, ``image_map`` (page → list of
    image refs), ``blocks`` (text/equation/figure/table with bbox+page_idx)
    and ``figures``. The review UI consumes ``blocks`` for bbox overlays;
    ``figures`` feeds the asset gallery.

    Cleans up MinerU's scratch dir on the way out — extracted PNG/JPG
    figures are copied into ``uploads/ocr_artifacts/<hash_short>_images/``
    so they outlive the run.
    """
    import asyncio as _asyncio
    import shutil as _shutil
    from pathlib import Path as _Path

    from app.benchmark.engines.mineru_engine import MinerUEngine
    from app.services.k12_batch.pipeline import _read_content_list

    # Wire MinerU env knobs (override .env values that ship a 600s shortcut).
    # Cap at the caller's remaining budget (parser adaptive timeout) so the CLI
    # subprocess is killed (process-tree kill in run_command_template) at the
    # same time the caller gives up — no 30-min orphan chewing CPU/RAM.
    timeout_val = int(os.getenv("MINERU_TIMEOUT_SECONDS", "1800"))
    if budget_seconds is not None:
        timeout_val = max(30, min(timeout_val, int(budget_seconds)))
    os.environ["OCR_BENCHMARK_PER_ENGINE_TIMEOUT"] = str(timeout_val)
    os.environ["MINERU_BENCHMARK_TIMEOUT_SECONDS"] = str(timeout_val)
    os.environ.setdefault("MINERU_METHOD", "ocr")
    os.environ.setdefault("MINERU_BACKEND", "pipeline")

    engine = MinerUEngine()
    if not engine.is_available():
        raise RuntimeError(
            "MinerU not available — install `mineru` CLI or set MINERU_CMD env var"
        )

    work_dir = ARTIFACT_DIR / f"_mineru_{file_hash[:16]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    # Persist extracted figures under media_uploads/ so the FE can fetch
    # them via /media/ocr_images/<hash>/<file> without leaking absolute paths.
    media_root = Path("media_uploads") / "ocr_images" / file_hash
    media_root.mkdir(parents=True, exist_ok=True)
    images_dir = media_root

    # MinerUEngine.run is synchronous (CLI subprocess); offload to a thread.
    try:
        result = await _asyncio.to_thread(engine.run, _Path(file_path), work_dir)
        if result.status != "success":
            raise RuntimeError(f"MinerU OCR failed: {result.error}")
        markdown = result.markdown or ""
        content_list = _read_content_list(work_dir)
    except BaseException:
        _shutil.rmtree(work_dir, ignore_errors=True)
        raise

    blocks: list[dict[str, Any]] = []
    figures: list[dict[str, Any]] = []
    # str → str: placeholder/url string used both for `placeholder in q["text"]`
    # match in pipeline.step2_preprocess (line 718) and FE rendering. INT keys
    # (page_num) here would crash that check with `'in <string>' requires
    # string as left operand, not int`.
    image_map: dict[str, str] = {}
    max_page = 0
    mineru_warnings: list[str] = []
    malformed_items = 0
    unknown_kinds: set[str] = set()

    # Emit Marker-legacy block shape so review_workflow + page-assets endpoint
    # consume MinerU output as drop-in replacement for Marker:
    #   block: {block_id, page_num (1-indexed), order, text, source, bbox, kind}
    #   figure: {figure_id, page_num, bbox, placeholder, source, caption}
    for idx, item in enumerate(content_list):
        # Validate shape — malformed items used to vanish silently (no warning).
        if not isinstance(item, dict):
            malformed_items += 1
            continue
        kind_raw = str(item.get("type") or "")
        page_idx = int(item.get("page_idx", 0) or 0)
        page_num = page_idx + 1  # 1-indexed for UI / DB.page_num
        bbox = item.get("bbox")
        block_id = f"p{page_num}_mineru_{idx}"
        max_page = max(max_page, page_num)

        if kind_raw == "text":
            blocks.append({
                "block_id": block_id,
                "page_num": page_num,
                "order": len(blocks),
                "text": item.get("text") or "",
                "source": "mineru",
                "bbox": bbox,
                "kind": "text",
            })
        elif kind_raw == "equation":
            blocks.append({
                "block_id": block_id,
                "page_num": page_num,
                "order": len(blocks),
                "text": item.get("text") or "",
                "source": "mineru",
                "bbox": bbox,
                "kind": "equation",
            })
        elif kind_raw == "table":
            blocks.append({
                "block_id": block_id,
                "page_num": page_num,
                "order": len(blocks),
                "text": str(item.get("table_body") or ""),
                "source": "mineru",
                "bbox": bbox,
                "kind": "table",
            })
        elif kind_raw == "image":
            img_path = str(item.get("img_path") or "").strip()
            caption = " ".join(item.get("image_caption") or []) or None
            # Copy the image into media_uploads/ocr_images/<hash>/ and
            # rewrite the markdown reference to the public /media URL the
            # frontend can load directly. We rewrite BOTH the markdown text
            # and the figure metadata so Gemini sees a URL (not a Windows
            # absolute path) when it preserves the image link, and the FE
            # can render <img src="/media/..."> on the questions page.
            # Path-traversal-guarded copy (refuses sources outside work_dir).
            new_url = _safe_relocate_image(img_path, work_dir, file_hash) if img_path else None
            if new_url and img_path:
                markdown = markdown.replace(img_path, new_url)
            new_url = new_url or ""
            placeholder = _Path(new_url or img_path).name if (new_url or img_path) else None
            figure_id = f"p{page_num}_mineru_fig_{len(figures) + 1}"
            figures.append({
                "figure_id": figure_id,
                "page_num": page_num,
                "bbox": bbox,
                "placeholder": placeholder,
                "url": new_url or None,
                "source": "mineru",
                "caption": caption,
            })
            blocks.append({
                "block_id": block_id,
                "page_num": page_num,
                "order": len(blocks),
                "text": caption or "",
                "source": "mineru",
                "bbox": bbox,
                "kind": "figure",
            })
            # Use the markdown-visible string (URL or path) as the key so
            # pipeline.step2_preprocess can do `placeholder in q["text"]`.
            ref = new_url or img_path
            if ref:
                image_map[ref] = new_url or img_path
        elif kind_raw:
            # Unrecognized block type — used to be dropped with no trace.
            unknown_kinds.add(kind_raw)

    _shutil.rmtree(work_dir, ignore_errors=True)

    # Surface silent omissions so callers/the artifact can flag review.
    if not content_list:
        mineru_warnings.append("mineru_content_list_empty: no structured blocks parsed")
    if malformed_items:
        mineru_warnings.append(f"mineru_malformed_items={malformed_items}")
    if unknown_kinds:
        mineru_warnings.append(f"mineru_unknown_block_kinds={sorted(unknown_kinds)}")

    return {
        "text": markdown,
        "page_count": max_page or 1,
        "image_map": image_map,
        "blocks": blocks,
        "figures": figures,
        "warnings": mineru_warnings,
    }


# Paddle layout→review-block conversion lives in app.services.paddle_vl_common
# (shared with the remote API client). Aliases kept for back-compat (tests import
# _paddle_blocks_to_review_shape / _PADDLE_LABEL_KIND).
from app.services.paddle_vl_common import (  # noqa: E402
    PADDLE_LABEL_KIND as _PADDLE_LABEL_KIND,
    blocks_to_review_shape as _paddle_blocks_to_review_shape,
)


@_ocr_limited
async def _run_paddle_vl_pipeline(
    file_path: str, file_hash: str, budget_seconds: float | None = None
) -> dict[str, Any]:
    """PaddleOCR-VL — markdown WITH LaTeX + Vietnamese, production shape.

    Reuses the benchmark ``PaddleEngine`` (PaddleOCR-VL) and converts its
    ``OcrResult`` into the same dict shape as :func:`_run_mineru_pipeline`:
    ``{text, page_count, image_map, blocks, figures, warnings}``. Layout blocks
    (label + normalized bbox) are parsed from PaddleOCR-VL's ``parsing_res_list``
    so the review overlay survives; if the engine yields no structured layout the
    block list is empty and segmentation falls back to native PDF geometry.

    ``PADDLE_VL_MODE=api`` → delegate to the remote hosted API (no local paddle/GPU).
    """
    # Remote API mode: offload to the hosted PaddleOCR-VL service. Import is lazy
    # so the local path never pulls the (heavy) paddle engine, and vice-versa.
    if PADDLE_VL_MODE == "api":
        from app.services.paddle_vl_api import run_paddle_vl_api

        return await run_paddle_vl_api(file_path, file_hash, budget_seconds=budget_seconds)

    import asyncio as _asyncio
    import shutil as _shutil
    from pathlib import Path as _Path

    from app.benchmark.engines.paddle_engine import PaddleEngine

    empty = {"text": "", "page_count": 0, "image_map": {}, "blocks": [], "figures": [], "warnings": []}
    work_dir = ARTIFACT_DIR / f"_paddlevl_{file_hash[:16]}"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        engine = PaddleEngine()
        if not engine.is_available():
            return {**empty, "warnings": ["paddle_vl_unavailable"]}
        result = await _asyncio.to_thread(engine.run, _Path(file_path), work_dir)
        if getattr(result, "status", None) != "success":
            return {**empty, "warnings": [f"paddle_vl_failed: {getattr(result, 'error', 'unknown')}"]}
        markdown = result.markdown or ""
        markdown, image_map, figures = _relocate_markdown_images(markdown, work_dir, file_hash)
        raw = result.raw or {}
        try:
            page_count = int(raw.get("pages") or 0)
        except Exception:
            page_count = 0
        blocks = _paddle_blocks_to_review_shape(raw.get("layout_blocks") or [])
        warnings: list[str] = []
        if not blocks:
            warnings.append("paddle_vl_no_layout_blocks: overlay dùng native PDF geometry")
        return {
            "text": markdown,
            "page_count": page_count or 1,
            "image_map": image_map,
            "blocks": blocks,
            "figures": figures,
            "warnings": warnings,
        }
    finally:
        _shutil.rmtree(work_dir, ignore_errors=True)


def _fallback_beats_primary(
    primary: dict | None,
    fallback: dict | None,
    prim_qual: dict,
    fb_qual: dict,
    prim_latex: dict,
    fb_latex: dict,
    is_stem: bool,
) -> bool:
    """Decide whether the fallback engine output should replace the primary.

    Engine-neutral (works for either MinerU→Paddle or Paddle→MinerU). Rules:
    never switch to an empty fallback; take it if primary is missing/empty; for
    STEM prefer math fidelity (un-broken LaTeX / higher latex_ratio); else prefer
    higher OCR quality_score. Ties keep the primary.
    """
    fb_text = (fallback or {}).get("text", "") or ""
    if len(fb_text.strip()) < EMPTY_TEXT_THRESHOLD:
        return False
    prim_text = (primary or {}).get("text", "") or ""
    if not primary or len(prim_text.strip()) < EMPTY_TEXT_THRESHOLD:
        return True
    if is_stem:
        if prim_latex.get("is_math_broken") and not fb_latex.get("is_math_broken"):
            return True
        if fb_latex.get("latex_ratio", 0) > prim_latex.get("latex_ratio", 0) + 0.05:
            return True
    return fb_qual.get("score", 0) > prim_qual.get("score", 0) + 0.05


async def _run_one_engine(
    name: str, file_path: str, file_hash: str, warnings: list[str],
    budget_seconds: float | None = None,
) -> dict[str, Any] | None:
    """Run a single OCR engine by name. Returns its output dict or ``None`` on
    failure/unavailability (messages pushed to ``warnings``). Never raises."""
    if name == "paddle-vl" and not _paddle_vl_available():
        warnings.append(
            "ocr_engine_unavailable: PaddleOCR-VL chưa cài "
            "(pip install paddleocr>=3.6.0 + PADDLE_USE_VL=1)"
        )
        return None
    try:
        out = (
            await _run_paddle_vl_pipeline(file_path, file_hash, budget_seconds=budget_seconds)
            if name == "paddle-vl"
            else await _run_mineru_pipeline(file_path, file_hash, budget_seconds=budget_seconds)
        )
        warnings.extend(out.get("warnings") or [])
        return out
    except Exception as exc:
        logger.warning("OCR engine %s failed for %s: %s", name, file_path, exc)
        warnings.append(f"{name.replace('-', '_')}_failed: {exc}")
        return None


async def _run_ocr_cascade(
    primary: str,
    fallback: str | None,
    *,
    file_path: str,
    file_hash: str,
    subject_code: str | None,
    is_stem: bool,
    warnings: list[str],
    quality_gated: bool,
    timeout_budget: float | None = None,
) -> tuple[dict[str, Any] | None, str, dict, dict, list[str]]:
    """Run ``primary`` OCR then optionally ``fallback``; return the winner.

    ``quality_gated=True`` (engine=auto): fallback runs when primary is empty /
    low-quality / STEM-math-broken and replaces it only if
    :func:`_fallback_beats_primary`. ``quality_gated=False`` (forced primary):
    fallback runs ONLY as a safety net when primary text is empty and then wins
    unconditionally if non-empty. ``fallback=None`` → forced single engine.

    Returns ``(chosen, ocr_method, quality, latex_final, methods)``.
    """
    methods: list[str] = []
    _t0 = time.monotonic()
    logger.info("OCR primary → %s for %s", primary, file_path)
    prim_out = await _run_one_engine(
        primary, file_path, file_hash, warnings, budget_seconds=timeout_budget
    )
    if prim_out is not None:
        methods.append(primary)
    p_text = (prim_out or {}).get("text", "") or ""
    quality = assess_ocr_quality(p_text, subject_code)
    latex_final = assess_latex_quality(p_text, subject_code)
    prim_empty = prim_out is None or len(p_text.strip()) < EMPTY_TEXT_THRESHOLD

    if not fallback:
        if prim_empty:
            warnings.append(f"{primary}_insufficient_no_fallback")
        return prim_out, primary, quality, latex_final, methods

    needs_fallback = prim_empty or (
        quality_gated
        and (quality["is_low_quality"] or (is_stem and latex_final.get("is_math_broken")))
    )
    if not needs_fallback or not OCR_FALLBACK_ENABLED:
        return prim_out, primary, quality, latex_final, methods

    logger.info("OCR fallback → %s (primary %s insufficient) for %s", fallback, primary, file_path)
    # Fallback chỉ còn phần budget chưa dùng (primary đã tiêu một phần).
    fb_budget = None
    if timeout_budget is not None:
        fb_budget = max(30.0, timeout_budget - (time.monotonic() - _t0))
    fb_out = await _run_one_engine(
        fallback, file_path, file_hash, warnings, budget_seconds=fb_budget
    )
    if fb_out is None:
        return prim_out, primary, quality, latex_final, methods
    fb_text = fb_out.get("text", "") or ""
    fb_quality = assess_ocr_quality(fb_text, subject_code)
    fb_latex = assess_latex_quality(fb_text, subject_code)

    if quality_gated:
        take = _fallback_beats_primary(
            prim_out, fb_out, quality, fb_quality, latex_final, fb_latex, is_stem
        )
    else:
        # forced-primary safety net: take fallback only when it actually has text.
        take = len(fb_text.strip()) >= EMPTY_TEXT_THRESHOLD

    if take:
        methods.append(fallback)
        return fb_out, fallback, fb_quality, fb_latex, methods
    methods.append(f"{fallback}-considered")
    return prim_out, primary, quality, latex_final, methods


async def extract_local_ocr_artifact(
    file_path: str,
    subject_code: str | None,
    *,
    use_cache: bool = True,
    grade: int | None = None,
    engine: str = "auto",
    timeout: float | None = None,
) -> dict[str, Any]:
    """K12 OCR pipeline — engine-selectable.

    ``engine``:
      - ``auto`` (mặc định): primary = ``OCR_PRIMARY_ENGINE`` (mặc định
        PaddleOCR-VL-1.6) + fallback engine còn lại (MinerU) theo quality-gate.
      - ``mineru``: chỉ MinerU (không fallback).
      - ``paddle-vl``: ép PaddleOCR-VL primary, MinerU chỉ là safety-net khi rỗng.

    PaddleOCR-VL-1.6 là OCR chính (self-host, giữ LaTeX + tiếng Việt); MinerU
    giữ vai trò baseline/fallback. Diacritic restoration delegated to
    ``step3_classify`` (Gemini) via the parse prompt in :mod:`subject_prompts`,
    so we no longer post-process text here. Cache schema ``v16`` invalidates
    artifacts from the old MinerU-primary cascade.

    ``grade`` is accepted (forwarded by /parse) for future use but not
    consumed at the OCR layer — Gemini classify reads it from the parent
    request context.
    """
    del grade  # acknowledge param; reserved for future grade-aware tuning
    engine = (engine or "auto").lower()
    if engine not in {"auto", "mineru", "paddle-vl"}:
        engine = "auto"
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    file_hash = await compute_file_hash(file_path)
    artifact_path = _artifact_path(file_hash, subject_code, engine)

    if use_cache and artifact_path.exists():
        try:
            cached = json.loads(artifact_path.read_text(encoding="utf-8"))
            cached["cache_hit"] = True
            cached["artifact_path"] = str(artifact_path)
            return cached
        except Exception as exc:
            logger.warning("Failed to read OCR artifact cache %s: %s", artifact_path, exc)

    warnings: list[str] = []
    ext = os.path.splitext(file_path)[1].lower()
    native_text_math_audit = assess_native_math_text(
        _read_native_pdf_text(file_path) if ext == ".pdf" else "",
        subject_code,
    )

    # ── OCR theo engine đã chọn. KHÔNG bao giờ crash: lỗi → artifact is_empty. ──
    #   auto      → primary=OCR_PRIMARY_ENGINE (mặc định paddle-vl) + fallback engine
    #               còn lại (quality-gate qua _fallback_beats_primary).
    #   paddle-vl → ép PaddleOCR-VL; MinerU chỉ là safety-net khi primary rỗng/lỗi.
    #   mineru    → ép MinerU, không fallback.
    is_stem = (subject_code or "").lower() in STEM_SUBJECTS
    marker_available = True  # legacy field for downstream compat

    if engine == "auto":
        primary = OCR_PRIMARY_ENGINE if OCR_PRIMARY_ENGINE in {"paddle-vl", "mineru"} else "paddle-vl"
        fallback = "mineru" if primary == "paddle-vl" else "paddle-vl"
        chosen, ocr_method, quality, latex_final, fallback_methods = await _run_ocr_cascade(
            primary, fallback, file_path=file_path, file_hash=file_hash,
            subject_code=subject_code, is_stem=is_stem, warnings=warnings, quality_gated=True,
            timeout_budget=timeout,
        )
    elif engine == "paddle-vl":
        # Forced PaddleOCR-VL; MinerU safety-net chỉ khi primary rỗng/lỗi.
        chosen, ocr_method, quality, latex_final, fallback_methods = await _run_ocr_cascade(
            "paddle-vl", "mineru", file_path=file_path, file_hash=file_hash,
            subject_code=subject_code, is_stem=is_stem, warnings=warnings, quality_gated=False,
            timeout_budget=timeout,
        )
    else:  # engine == "mineru": forced single engine, no fallback.
        chosen, ocr_method, quality, latex_final, fallback_methods = await _run_ocr_cascade(
            "mineru", None, file_path=file_path, file_hash=file_hash,
            subject_code=subject_code, is_stem=is_stem, warnings=warnings, quality_gated=False,
            timeout_budget=timeout,
        )

    text = (chosen or {}).get("text", "") or ""
    is_empty = len(text.strip()) < EMPTY_TEXT_THRESHOLD
    if quality["is_low_quality"]:
        warnings.append(f"OCR quality low: {quality['reason']}")
    if is_empty:
        warnings.append(
            f"OCR extracted only {len(text.strip())} chars (<{EMPTY_TEXT_THRESHOLD}). "
            f"Engines tried: {fallback_methods or ['none']}"
        )
        logger.error("OCR empty/insufficient for %s (engines=%s)", file_path, fallback_methods)

    formula_audit = build_formula_audit(
        subject_code=subject_code,
        latex_quality=latex_final,
        native_text_math_audit=native_text_math_audit,
        ocr_method=ocr_method,
    )
    # publishable phải tôn trọng is_empty (CRITICAL): extraction rỗng KHÔNG publishable.
    document_status = "needs_review" if (formula_audit["review_required"] or is_empty) else "high_confidence"
    publishable = not (formula_audit["review_required"] or is_empty)
    if latex_final["is_math_broken"]:
        warnings.append(
            f"STEM doc: math không đầy đủ LaTeX "
            f"(LaTeX ratio={latex_final['latex_ratio']:.0%}, "
            f"plain math hints={latex_final['plain_math_hint_count']})"
        )
    if native_text_math_audit["is_corrupted"]:
        warnings.append(
            "Native PDF text layer có dấu hiệu làm hỏng cấu trúc công thức; "
            "ưu tiên kết quả OCR math-aware thay vì text extraction thô."
        )
    if formula_audit["review_required"]:
        warnings.append(
            "Formula audit yêu cầu rà soát trước khi coi tài liệu là high-confidence."
        )

    chosen = chosen or {}
    artifact = {
        "text": text,
        "markdown": text,
        "method": ocr_method,
        "page_count": chosen.get("page_count", 0) or 1,
        "char_count": len(text or ""),
        "quality_score": quality["score"],
        "quality_reason": quality["reason"],
        "is_low_quality": quality["is_low_quality"],
        "is_empty": is_empty,
        # LaTeX/math quality cho STEM docs (Sprint 5 — math optimization)
        "latex_quality": latex_final,
        # Formula fidelity pilot (v7): diagnostics only; public question schema unchanged.
        "native_text_math_audit": native_text_math_audit,
        "formula_audit": formula_audit,
        "document_status": document_status,
        "publishable": publishable,
        # Legacy field — keep True so old "marker missing" warnings don't fire.
        "marker_available": marker_available,
        "fallbacks_used": fallback_methods,
        "image_map": chosen.get("image_map", {}) or {},
        "blocks": chosen.get("blocks", []) or [],
        "figures": chosen.get("figures", []) or [],
        "layout_source": ocr_method,
        "file_hash": file_hash,
        "artifact_path": str(artifact_path),
        "cache_hit": False,
        "warnings": warnings,
    }

    # Không cache kết quả rỗng — tránh "đóng băng" lỗi tạm thời (vd MinerU down)
    # theo file_hash khiến re-upload không bao giờ thử lại engine.
    if not is_empty:
        try:
            artifact_path.write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to write OCR artifact cache %s: %s", artifact_path, exc)

    return artifact
