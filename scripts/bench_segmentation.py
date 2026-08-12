from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from app.services.pipeline import _run_document_segmentation


@dataclass
class EvalRow:
    file_id: str
    expected_document_type: str
    expected_question_count: int
    actual_question_count: int
    question_count_correct: bool
    boundary_exact_match_rate: float
    wrong_answer_attachment_rate: float
    wrong_solution_attachment_rate: float
    false_question_generated: bool
    extra_text_ratio: float
    document_type: str
    confidence: float
    warnings: str


def evaluate_annotation(path: Path) -> EvalRow:
    payload = json.loads(path.read_text(encoding="utf-8"))
    ocr_text = _load_ocr_text(path, payload)
    expected = payload.get("questions", [])
    actual, report = _run_document_segmentation(
        {"text": ocr_text, "image_map": {}, "method": payload.get("ocr_method", "fixture")}
    )

    expected_by_num = {int(q["cau_num"]): q for q in expected}
    actual_by_num = {int(q["cau_num"]): q for q in actual}
    shared_nums = sorted(set(expected_by_num) & set(actual_by_num))

    boundary_matches = sum(
        _norm(expected_by_num[num].get("expected_question_text", ""))
        == _norm(actual_by_num[num].get("text", ""))
        for num in shared_nums
    )
    wrong_answers = sum(
        _norm(expected_by_num[num].get("expected_answer"))
        != _norm(actual_by_num[num].get("answer"))
        for num in shared_nums
        if actual_by_num[num].get("answer") is not None
    )
    wrong_solutions = sum(
        _norm_join(expected_by_num[num].get("expected_solution_steps", []))
        != _norm_join(actual_by_num[num].get("solution_steps", []))
        for num in shared_nums
        if actual_by_num[num].get("solution_steps")
    )
    denominator = max(len(shared_nums), 1)
    expected_chars = sum(len(_norm(q.get("expected_question_text", ""))) for q in expected)
    actual_chars = sum(len(_norm(q.get("text", ""))) for q in actual)
    extra_text_ratio = max(0, actual_chars - expected_chars) / max(expected_chars, 1)
    expected_document_type = str(payload.get("document_type") or "")

    return EvalRow(
        file_id=str(payload.get("file_id") or path.stem),
        expected_document_type=expected_document_type,
        expected_question_count=len(expected),
        actual_question_count=len(actual),
        question_count_correct=len(expected) == len(actual),
        boundary_exact_match_rate=boundary_matches / denominator,
        wrong_answer_attachment_rate=wrong_answers / denominator,
        wrong_solution_attachment_rate=wrong_solutions / denominator,
        false_question_generated=expected_document_type == "no_questions" and bool(actual),
        extra_text_ratio=extra_text_ratio,
        document_type=str(report["document_type"]),
        confidence=float(report["confidence"]),
        warnings="|".join(report["warnings"]),
    )


def _load_ocr_text(annotation_path: Path, payload: dict[str, Any]) -> str:
    if "ocr_text" in payload:
        return str(payload["ocr_text"])
    rel = payload.get("ocr_text_path")
    if rel:
        return (annotation_path.parent / rel).read_text(encoding="utf-8")
    raise ValueError(f"{annotation_path}: expected `ocr_text` or `ocr_text_path`")


def _norm(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip().lower()


def _norm_join(values: list[str]) -> str:
    return _norm("\n".join(values or []))


def write_csv(rows: list[EvalRow], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(EvalRow.__dataclass_fields__))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def build_summary(rows: list[EvalRow]) -> dict[str, Any]:
    if not rows:
        return {"file_count": 0}
    return {
        "file_count": len(rows),
        "question_count_accuracy": mean(row.question_count_correct for row in rows),
        "question_boundary_exact_match_rate": mean(row.boundary_exact_match_rate for row in rows),
        "wrong_answer_attachment_rate": mean(row.wrong_answer_attachment_rate for row in rows),
        "wrong_solution_attachment_rate": mean(row.wrong_solution_attachment_rate for row in rows),
        "false_question_generation_rate": mean(row.false_question_generated for row in rows),
        "mean_extra_text_ratio": mean(row.extra_text_ratio for row in rows),
        "mean_confidence": mean(row.confidence for row in rows),
    }


def gate(summary: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if summary.get("question_count_accuracy", 0.0) < 0.95:
        failures.append("question_count_accuracy < 0.95")
    if summary.get("question_boundary_exact_match_rate", 0.0) < 0.95:
        failures.append("question_boundary_exact_match_rate < 0.95")
    if summary.get("wrong_answer_attachment_rate", 1.0) > 0.01:
        failures.append("wrong_answer_attachment_rate > 0.01")
    if summary.get("wrong_solution_attachment_rate", 1.0) > 0.01:
        failures.append("wrong_solution_attachment_rate > 0.01")
    if summary.get("false_question_generation_rate", 1.0) > 0.0:
        failures.append("false_question_generation_rate > 0")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate document segmentation annotations.")
    parser.add_argument("--annotations-dir", type=Path, required=True)
    parser.add_argument("--csv-out", type=Path, default=Path("bench_segmentation.csv"))
    parser.add_argument("--json-out", type=Path, default=Path("bench_segmentation.json"))
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args()

    rows = [evaluate_annotation(path) for path in sorted(args.annotations_dir.glob("*.json"))]
    summary = build_summary(rows)
    write_csv(rows, args.csv_out)
    args.json_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    failures = gate(summary) if args.gate else []
    if failures:
        print("Gate failed:", "; ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
