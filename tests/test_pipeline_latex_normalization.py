import app.services.pipeline as pipeline

# step3_classify (Gemini classify-only) đã gỡ 2026-07-10 cùng test của nó —
# LaTeX normalization giờ nằm trong prompt full-doc của ai_parser.parse().


def test_step2_marks_native_pdf_questions_for_latex_normalization(monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEGMENTATION_ENABLED", "0")
    monkeypatch.setenv("DOCUMENT_SEGMENTATION_SHADOW", "0")

    result = pipeline.step2_preprocess(
        {
            "text": "Cau 1. Tinh A = 1/2 + x2.",
            "image_map": {},
            "requires_latex_normalization": True,
        }
    )

    assert result
    assert result[0]["needs_latex_normalization"] is True
