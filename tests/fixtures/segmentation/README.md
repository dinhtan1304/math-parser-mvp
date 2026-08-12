# Segmentation annotation fixture format

Each `.json` file represents one OCR-normalized educational document.

```json
{
  "file_id": "sample_exam_001",
  "document_type": "exam_with_full_solutions",
  "layout_type": "single_column",
  "answer_location": "end_of_file",
  "subject": "toan",
  "ocr_text_path": "sample_exam_001.txt",
  "questions": [
    {
      "cau_num": 1,
      "expected_question_text": "## Câu 1\nTính $1+1$.",
      "expected_answer": "2",
      "expected_solution_steps": ["Đáp số: 2"]
    }
  ]
}
```

The benchmark accepts either `ocr_text_path` or inline `ocr_text`.  Real rollout
corpus files should be curated outside unit-test fixtures and follow this same
shape so `scripts/bench_segmentation.py` can evaluate them unchanged.
