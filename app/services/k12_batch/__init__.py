"""K12 batch OCR pipeline.

CLI batch processor for Vietnamese K12 exam PDFs:
  MinerU OCR → LaTeX validation + PaddleOCR-VL fallback
  → regex segmentation → Gemini finalization (AIQuestionParser)
  → AnswerExtractor merge → JSON (GeneratedQuestion schema).

Entry point: `scripts/ocr_k12_batch.py`.
"""
