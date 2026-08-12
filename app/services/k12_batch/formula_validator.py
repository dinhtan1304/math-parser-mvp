"""Formula validator + PaddleOCR-VL crop retry.

Validates `$$...$$` and inline `$...$` LaTeX blocks in OCR markdown using
latex2mathml. For each invalid block that has a known bbox (from MinerU's
``content_list.json``), renders the PDF page, crops the bbox region, and
re-runs PaddleOCR-VL on the crop to obtain a cleaner LaTeX string.

Public API
----------
* ``validate_latex(latex)`` — quick parse check.
* ``find_invalid_formulas(markdown)`` — locate broken blocks in markdown.
* ``crop_formula_from_pdf(pdf, page_idx, bbox, out_png)`` — write crop PNG.
* ``retry_with_paddle_vl(crop_png)`` — call PaddleOCR-VL on the crop.
* ``revalidate_markdown(markdown, content_list, pdf, workdir)`` — pipeline
  entry: scans markdown, attempts retries, returns ``(new_md, stats)``.
"""

from __future__ import annotations

import logging
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_RE_BLOCK_LATEX = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_RE_INLINE_LATEX = re.compile(r"(?<!\$)\$([^$\n]{1,400}?)\$(?!\$)")


@dataclass
class FormulaIssue:
    """A LaTeX block that failed validation."""

    latex: str
    start: int
    end: int
    kind: str  # 'block' | 'inline'
    error: str


@dataclass
class FormulaRetryStats:
    scanned: int = 0
    invalid: int = 0
    retried: int = 0
    recovered: int = 0
    paddle_empty: int = 0
    no_bbox: int = 0
    crop_failed: int = 0
    retries_detail: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "invalid": self.invalid,
            "retried": self.retried,
            "recovered": self.recovered,
            "paddle_empty": self.paddle_empty,
            "no_bbox": self.no_bbox,
            "crop_failed": self.crop_failed,
            "retries_detail": self.retries_detail,
        }


_TWO_ARG_COMMANDS = ("\\frac", "\\dfrac", "\\tfrac", "\\binom", "\\dbinom", "\\overset", "\\underset")


def _check_structural(latex: str) -> str | None:
    """Cheap structural checks latex2mathml is too lenient to catch.

    Returns ``None`` if the formula passes, otherwise a short error label.
    Catches the OCR-typical failure modes: unbalanced braces, dangling
    backslash, `\\frac` with the wrong arity, etc.
    """
    s = latex.strip()
    if not s:
        return "empty"
    # Truncation markers.
    if s.endswith("\\"):
        return "trailing_backslash"
    if s.endswith(("+", "-", "=", "/", "*", ",", ";", "_", "^")):
        return "trailing_operator"
    # Brace / bracket balance.
    depth_curly = 0
    depth_square = 0
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            i += 2
            continue
        if ch == "{":
            depth_curly += 1
        elif ch == "}":
            depth_curly -= 1
            if depth_curly < 0:
                return "extra_close_brace"
        elif ch == "[":
            depth_square += 1
        elif ch == "]":
            depth_square -= 1
            if depth_square < 0:
                return "extra_close_square"
        i += 1
    if depth_curly != 0:
        return "unbalanced_curly"
    if depth_square != 0:
        return "unbalanced_square"
    # Two-argument commands missing their second argument.
    for cmd in _TWO_ARG_COMMANDS:
        for m in re.finditer(re.escape(cmd) + r"\b", s):
            tail = s[m.end():].lstrip()
            # Must have two {…} groups immediately following.
            if not tail.startswith("{"):
                return f"{cmd}_missing_arg1"
            # Walk past first {…}.
            d = 0
            j = 0
            while j < len(tail):
                c = tail[j]
                if c == "\\" and j + 1 < len(tail):
                    j += 2
                    continue
                if c == "{":
                    d += 1
                elif c == "}":
                    d -= 1
                    if d == 0:
                        break
                j += 1
            rest = tail[j + 1:].lstrip() if j < len(tail) else ""
            if not rest.startswith("{"):
                return f"{cmd}_missing_arg2"
    return None


def validate_latex(latex: str) -> tuple[bool, str | None]:
    """Two-stage validation: structural heuristics + latex2mathml render.

    latex2mathml alone is too lenient (it renders nonsense rather than
    rejecting it), so we layer cheap structural checks on top to catch the
    OCR-typical failure modes (unbalanced braces, truncated formulas,
    ``\\frac`` with a missing argument).
    """
    if not latex or not latex.strip():
        return False, "empty"
    err = _check_structural(latex)
    if err:
        return False, err
    try:
        from latex2mathml.converter import convert
    except ImportError as exc:
        logger.error("latex2mathml not installed: %s", exc)
        return True, None
    try:
        convert(latex.strip())
        return True, None
    except Exception as exc:
        msg = type(exc).__name__ + ": " + str(exc)[:200]
        return False, msg


def find_invalid_formulas(markdown: str) -> list[FormulaIssue]:
    """Scan markdown for `$$...$$` and `$...$` blocks that fail validation.

    Only formulas longer than 2 chars are reported (single-letter `$x$`
    snippets are too short to bother validating).
    """
    issues: list[FormulaIssue] = []
    for match in _RE_BLOCK_LATEX.finditer(markdown):
        latex = match.group(1).strip()
        if len(latex) < 2:
            continue
        ok, err = validate_latex(latex)
        if not ok:
            issues.append(
                FormulaIssue(latex=latex, start=match.start(), end=match.end(), kind="block", error=err or "?")
            )
    for match in _RE_INLINE_LATEX.finditer(markdown):
        latex = match.group(1).strip()
        if len(latex) < 3:  # inline noise threshold a bit higher
            continue
        ok, err = validate_latex(latex)
        if not ok:
            issues.append(
                FormulaIssue(latex=latex, start=match.start(), end=match.end(), kind="inline", error=err or "?")
            )
    return issues


def _normalize_bbox(bbox: list[float], page_w: float, page_h: float) -> tuple[float, float, float, float]:
    """Map bbox to PDF point coordinates.

    Handles three conventions seen in the wild:
      1. normalized to [0, 1] — multiply by page dims;
      2. already in PDF points — use directly;
      3. rendered pixels at 200 DPI — scale by 72/200.
    """
    if not bbox or len(bbox) < 4:
        raise ValueError(f"invalid bbox: {bbox!r}")
    x0, y0, x1, y1 = [float(v) for v in bbox[:4]]
    max_val = max(abs(x0), abs(y0), abs(x1), abs(y1))
    if max_val <= 1.5:
        return x0 * page_w, y0 * page_h, x1 * page_w, y1 * page_h
    if max_val <= max(page_w, page_h) * 1.1:
        return x0, y0, x1, y1
    # Assume rendered at 200 DPI (MinerU default for inference).
    scale = 72.0 / 200.0
    return x0 * scale, y0 * scale, x1 * scale, y1 * scale


def crop_formula_from_pdf(pdf_path: Path, page_idx: int, bbox: list[float], out_png: Path) -> bool:
    """Render the PDF page and write the bbox region to ``out_png``.

    Returns ``True`` on success, ``False`` if the page or bbox is unusable.
    Uses PyMuPDF (already a project dependency).
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.error("PyMuPDF (fitz) not available; cannot crop formulas")
        return False
    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        logger.warning("crop_formula: cannot open %s: %s", pdf_path, exc)
        return False
    try:
        if page_idx < 0 or page_idx >= doc.page_count:
            logger.warning("crop_formula: page_idx %d out of range (page_count=%d)", page_idx, doc.page_count)
            return False
        page = doc.load_page(page_idx)
        x0, y0, x1, y1 = _normalize_bbox(bbox, page.rect.width, page.rect.height)
        # Small padding around the formula helps the recognizer see context.
        pad = 4.0
        rect = fitz.Rect(
            max(0.0, x0 - pad),
            max(0.0, y0 - pad),
            min(page.rect.width, x1 + pad),
            min(page.rect.height, y1 + pad),
        )
        if rect.is_empty or rect.width < 4 or rect.height < 4:
            logger.warning("crop_formula: rect too small %s", rect)
            return False
        # Render at 300 DPI for crisper formula recognition.
        zoom = 300.0 / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, clip=rect, alpha=False)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(out_png))
        return True
    except Exception as exc:
        logger.exception("crop_formula failed: %s", exc)
        return False
    finally:
        try:
            doc.close()
        except Exception:
            pass


def retry_with_paddle_vl(crop_png: Path) -> str | None:
    """Run PaddleOCR-VL on a single cropped image and return cleaned LaTeX.

    Returns ``None`` if the pipeline is unavailable or yields empty output.
    Strips surrounding ``$$``/``$`` delimiters and whitespace.
    """
    try:
        from app.benchmark.engines.paddle_engine import _ensure_vl_pipeline
    except ImportError as exc:
        logger.error("paddle_engine import failed: %s", exc)
        return None
    try:
        pipeline = _ensure_vl_pipeline()
    except Exception as exc:
        logger.warning("PaddleOCR-VL pipeline init failed: %s", exc)
        return None

    try:
        results = pipeline.predict(str(crop_png))
    except Exception as exc:
        logger.warning("PaddleOCR-VL predict failed for %s: %s", crop_png, exc)
        return None

    parts: list[str] = []
    try:
        for res in results:
            md_obj = getattr(res, "markdown", None)
            if isinstance(md_obj, dict):
                parts.append(str(md_obj.get("markdown_texts") or md_obj.get("text") or ""))
            elif md_obj is not None:
                parts.append(str(md_obj))
    except Exception as exc:
        logger.warning("PaddleOCR-VL result iter failed: %s", exc)
        return None

    text = "\n".join(p for p in parts if p).strip()
    if not text:
        return None
    return _strip_latex_delims(text)


def _strip_latex_delims(text: str) -> str:
    """Strip outer ``$$...$$`` / ``$...$`` / ``\\(...\\)`` / ``\\[...\\]`` wrappers."""
    s = text.strip()
    for opener, closer in (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$")):
        if s.startswith(opener) and s.endswith(closer) and len(s) > len(opener) + len(closer):
            return s[len(opener) : -len(closer)].strip()
    return s


def _build_equation_bbox_map(content_list: list[dict[str, Any]]) -> dict[str, tuple[int, list[float]]]:
    """Map ``latex_text → (page_idx, bbox)`` from MinerU's ``content_list.json``.

    Keys are stripped LaTeX strings; ties on identical LaTeX keep the first
    occurrence (good enough — duplicates are rare and either is a valid crop
    target for re-OCR).
    """
    bbox_map: dict[str, tuple[int, list[float]]] = {}
    for item in content_list or []:
        if str(item.get("type") or "") != "equation":
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        latex = _strip_latex_delims(text)
        if not latex or latex in bbox_map:
            continue
        page = item.get("page_idx")
        bbox = item.get("bbox")
        if page is None or not bbox:
            continue
        try:
            bbox_map[latex] = (int(page), list(bbox))
        except (TypeError, ValueError):
            continue
    return bbox_map


def revalidate_markdown(
    markdown: str,
    content_list: list[dict[str, Any]],
    pdf_path: Path,
    workdir: Path,
    max_workers: int = 4,
    max_retries: int = 50,
) -> tuple[str, FormulaRetryStats]:
    """Validate every LaTeX block; crop+retry the invalid ones.

    ``content_list`` is the parsed ``content_list.json`` MinerU writes
    alongside its markdown. ``workdir`` is a directory the validator can
    use as scratch space for cropped PNGs (e.g. ``out_dir/_debug``).

    Validation runs in a thread pool because ``latex2mathml.convert`` is
    pure CPU work and the calls are independent.
    """
    stats = FormulaRetryStats()
    if not markdown:
        return markdown, stats

    issues = find_invalid_formulas(markdown)
    block_count = len(_RE_BLOCK_LATEX.findall(markdown))
    inline_count = len(_RE_INLINE_LATEX.findall(markdown))
    stats.scanned = block_count + inline_count
    stats.invalid = len(issues)

    if not issues:
        return markdown, stats

    bbox_map = _build_equation_bbox_map(content_list)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    crops_dir = workdir / "formula_crops"
    crops_dir.mkdir(parents=True, exist_ok=True)

    # Cap how much we retry — pathological docs can have hundreds of broken
    # snippets and we don't want to spend an hour cropping all of them.
    targets = issues[:max_retries]

    def _retry_one(idx: int, issue: FormulaIssue) -> tuple[int, FormulaIssue, str | None, str]:
        loc = bbox_map.get(issue.latex.strip())
        if loc is None:
            return idx, issue, None, "no_bbox"
        page_idx, bbox = loc
        out_png = crops_dir / f"formula_{idx:03d}_p{page_idx}.png"
        if not crop_formula_from_pdf(pdf_path, page_idx, bbox, out_png):
            return idx, issue, None, "crop_failed"
        new_latex = retry_with_paddle_vl(out_png)
        if not new_latex:
            return idx, issue, None, "paddle_empty"
        # Sanity: don't accept a "recovery" that itself fails validation.
        ok, _ = validate_latex(new_latex)
        if not ok:
            return idx, issue, new_latex, "invalid_after_retry"
        return idx, issue, new_latex, "recovered"

    replacements: list[tuple[int, int, str, str]] = []  # (start, end, original_block, new_block)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_retry_one, idx, issue) for idx, issue in enumerate(targets)]
        for fut in as_completed(futures):
            idx, issue, new_latex, status = fut.result()
            detail = {
                "idx": idx,
                "kind": issue.kind,
                "status": status,
                "original": issue.latex[:200],
                "recovered": (new_latex or "")[:200],
                "error": issue.error,
            }
            stats.retries_detail.append(detail)
            if status == "no_bbox":
                stats.no_bbox += 1
                continue
            if status == "crop_failed":
                stats.crop_failed += 1
                continue
            stats.retried += 1
            if status == "paddle_empty":
                stats.paddle_empty += 1
                continue
            if status == "recovered" and new_latex:
                stats.recovered += 1
                delim = "$$" if issue.kind == "block" else "$"
                new_block = f"{delim}{new_latex}{delim}"
                original_block = markdown[issue.start : issue.end]
                replacements.append((issue.start, issue.end, original_block, new_block))

    if not replacements:
        return markdown, stats

    # Apply replacements right-to-left so earlier offsets stay valid.
    replacements.sort(key=lambda r: r[0], reverse=True)
    new_markdown = markdown
    for start, end, _orig, new_block in replacements:
        new_markdown = new_markdown[:start] + new_block + new_markdown[end:]

    # Clean up crops — keep only if caller asked for _debug retention.
    try:
        if not (workdir.parent / "_debug").exists():
            shutil.rmtree(crops_dir, ignore_errors=True)
    except Exception:
        pass

    logger.info(
        "formula_validator: scanned=%d invalid=%d retried=%d recovered=%d (no_bbox=%d, crop_failed=%d, paddle_empty=%d)",
        stats.scanned, stats.invalid, stats.retried, stats.recovered,
        stats.no_bbox, stats.crop_failed, stats.paddle_empty,
    )
    return new_markdown, stats
