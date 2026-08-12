"""Output writer for the K12 batch pipeline.

Responsibilities:

* merge :class:`AnswerExtractor` results into the Gemini-parsed dicts
  so the ``answer`` field reflects the authoritative answer key when
  one was detected in the source PDF;
* validate every item against :class:`GeneratedQuestion` and drop the
  ones that fail (logging the reason);
* write the four artifacts the CLI promises: ``raw.md``,
  ``questions.json``, ``images/`` and ``report.json``.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from app.schemas.generator import GeneratedQuestion
from app.services.answer_extractor import AnswerExtractor, AnswerMap

logger = logging.getLogger(__name__)


def apply_answers(
    parsed: list[dict[str, Any]],
    answer_map: AnswerMap,
    confidence_threshold: float = 0.85,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Overwrite each item's ``answer`` with the extractor's answer when trustworthy.

    The extractor's answer wins only if its confidence meets the
    threshold AND the question number is mappable from the parsed
    item's text. Items the extractor has no answer for are left
    untouched.

    Returns ``(parsed, stats)`` where ``stats`` reports coverage so the
    pipeline can include it in ``report.json``.
    """
    if not parsed:
        return parsed, {"coverage": 0.0, "applied": 0, "extractor_confidence": 0.0}
    if not answer_map.answers or answer_map.confidence < confidence_threshold:
        # Still report what fraction of items came back with an answer.
        with_answer = sum(1 for item in parsed if str(item.get("answer") or "").strip())
        return parsed, {
            "coverage": round(with_answer / len(parsed), 3),
            "applied": 0,
            "extractor_confidence": round(answer_map.confidence, 3),
        }

    applied = 0
    for item in parsed:
        number = _question_number_from_item(item)
        if number is None:
            continue
        extracted = answer_map.answers.get(number)
        if not extracted:
            continue
        prior = str(item.get("answer") or "").strip()
        if prior == extracted:
            continue
        item["answer"] = extracted
        applied += 1

    with_answer = sum(1 for item in parsed if str(item.get("answer") or "").strip())
    return parsed, {
        "coverage": round(with_answer / len(parsed), 3),
        "applied": applied,
        "extractor_confidence": round(answer_map.confidence, 3),
        "extractor_source": answer_map.source,
    }


def _question_number_from_item(item: dict[str, Any]) -> int | None:
    import re

    text = str(item.get("question") or "")
    match = re.match(r"\s*(?:Câu|Bài|Question)?\s*(\d{1,3})\b", text, re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def run_answer_extractor(full_text: str, parsed: list[dict[str, Any]]) -> AnswerMap:
    """Adapt parsed dicts to the shape :class:`AnswerExtractor` expects.

    The extractor needs ``[{cau_num: int, text: str}, ...]``. We map by
    inferring the number from the question text — items whose number we
    can't parse are skipped (those rarely have a corresponding answer
    key entry anyway).
    """
    if not full_text or not parsed:
        return AnswerMap()
    bridge: list[dict[str, Any]] = []
    for idx, item in enumerate(parsed):
        number = _question_number_from_item(item) or (idx + 1)
        bridge.append({"cau_num": number, "text": str(item.get("question") or "")})
    extractor = AnswerExtractor()
    return extractor.extract(full_text, bridge)


def validate_against_schema(parsed: list[dict[str, Any]]) -> tuple[list[GeneratedQuestion], list[dict[str, Any]]]:
    """Run :class:`GeneratedQuestion.model_validate` on each item.

    Returns ``(valid_questions, validation_errors)``. ``validation_errors``
    contains ``{index, error, item_preview}`` for each failure, so the
    pipeline can log them and surface them in ``report.json``.
    """
    valid: list[GeneratedQuestion] = []
    errors: list[dict[str, Any]] = []
    for idx, item in enumerate(parsed):
        try:
            q = GeneratedQuestion.model_validate(item)
            valid.append(q)
        except Exception as exc:
            errors.append({
                "index": idx,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "item_preview": str(item.get("question") or "")[:200],
            })
    if errors:
        logger.warning("validate_against_schema: %d/%d items failed", len(errors), len(parsed))
    return valid, errors


def _copy_images(source_images: list[Path], target_dir: Path) -> list[str]:
    """Copy referenced image files to ``target_dir`` and return relative paths."""
    target_dir.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    for src in source_images:
        if not src.exists():
            continue
        dst = target_dir / src.name
        # Avoid overwriting if a same-named asset already came in earlier.
        if dst.exists() and dst.stat().st_size == src.stat().st_size:
            out.append(dst.name)
            continue
        if dst.exists():
            stem, suffix = dst.stem, dst.suffix
            i = 1
            while True:
                alt = target_dir / f"{stem}_{i}{suffix}"
                if not alt.exists():
                    dst = alt
                    break
                i += 1
        try:
            shutil.copy2(src, dst)
            out.append(dst.name)
        except Exception as exc:
            logger.warning("copy_images: failed %s -> %s: %s", src, dst, exc)
    return out


def write_outputs(
    out_dir: Path,
    raw_md: str,
    questions: list[GeneratedQuestion],
    images: list[Path],
    report: dict[str, Any],
    image_dirname: str = "images",
    raw_filename: str = "raw.md",
    json_filename: str = "questions.json",
    report_filename: str = "report.json",
    debug_payload: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Write all per-file artifacts to ``out_dir``.

    Returns a map of artifact name → absolute path string.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, str] = {}

    md_path = out_dir / raw_filename
    md_path.write_text(raw_md, encoding="utf-8")
    artifacts["raw_md"] = str(md_path)

    image_files: list[str] = []
    if images:
        target = out_dir / image_dirname
        image_files = _copy_images(images, target)
    artifacts["images_dir"] = str(out_dir / image_dirname)

    questions_payload = [_question_to_dict(q) for q in questions]
    qjson_path = out_dir / json_filename
    qjson_path.write_text(
        json.dumps(questions_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    artifacts["questions_json"] = str(qjson_path)

    full_report = {
        **report,
        "question_count": len(questions),
        "images_copied": len(image_files),
        "artifacts": {k: str(v) for k, v in artifacts.items()},
    }
    report_path = out_dir / report_filename
    report_path.write_text(
        json.dumps(full_report, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    artifacts["report_json"] = str(report_path)

    if debug_payload:
        debug_dir = out_dir / "_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        for name, payload in debug_payload.items():
            (debug_dir / f"{name}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
                encoding="utf-8",
            )

    return artifacts


def _question_to_dict(q: GeneratedQuestion) -> dict[str, Any]:
    return q.model_dump()


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
