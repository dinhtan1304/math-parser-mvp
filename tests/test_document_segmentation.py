from app.services.block_classifier import classify_blocks
from app.services.document_blocks import build_document_pages
from app.services.document_structure import build_document_structure
from app.services.pipeline import step2_preprocess
from app.services.question_assembler import assemble_questions


QUESTIONS_WITH_SOLUTIONS = """
# ĐỀ THI TOÁN

## Câu 1
Tính $1+1$.

## Câu 2
Giải phương trình $x+1=3$.

## HƯỚNG DẪN CHẤM

### Câu 1
Đáp số: 2

### Câu 2
Ta có $x=2$.
Đáp số: 2
"""


INLINE_SOLUTION_DOC = """
## Câu 1
Tính $2+2$.

Lời giải: Ta có $2+2=4$.
Đáp số: 4

## Câu 2
Tính $3+3$.
"""


NO_QUESTION_DOC = """
# Chuyên đề phân số

Phân số là số có dạng $a/b$ với $b \\ne 0$.

Muốn cộng hai phân số cùng mẫu, ta cộng các tử số và giữ nguyên mẫu số.
"""


BOLD_MARKDOWN_HSG_DOC = """
### **ĐỀ THI KHẢO SÁT**

## **Câu 1 (2,0 điểm)**
Tính $1+1$.

## **Câu 2 (2,0 điểm)**
Tính $2+2$.

### MÔN TOÁN 7 HƯỚNG DẪN CHẤM

### Câu 1
Đáp số: 2

### Câu 2
Đáp số: 4
"""


def _segment(text: str):
    pages = build_document_pages({"text": text, "method": "marker"})
    blocks = [block for page in pages for block in page.blocks]
    classifications = classify_blocks(blocks)
    parsed = build_document_structure(pages, classifications)
    assembled = assemble_questions(parsed, full_text=text)
    return pages, classifications, parsed, assembled


def test_build_document_pages_keeps_heading_and_page_markers():
    pages = build_document_pages(
        {
            "text": "[Trang 1]\n# ĐỀ THI\n\n## Câu 1\nNội dung\n\n[Trang 2]\n## Câu 2\nNội dung",
            "method": "marker",
        }
    )

    assert [page.page_num for page in pages] == [1, 2]
    assert pages[0].blocks[0].kind == "heading"
    assert pages[0].blocks[1].features["question_num"] == 1
    assert pages[1].blocks[0].features["question_num"] == 2


def test_end_of_document_solution_section_maps_to_matching_questions():
    _, _, parsed, assembled = _segment(QUESTIONS_WITH_SOLUTIONS)

    assert parsed.document_type == "exam_with_full_solutions"
    assert [q.cau_num for q in parsed.questions] == [1, 2]
    assert [q["answer"] for q in assembled.questions] == ["2", "2"]
    assert all(q["solution_steps"] for q in assembled.questions)


def test_inline_solution_is_not_misclassified_as_answer_section_header():
    _, classifications, parsed, assembled = _segment(INLINE_SOLUTION_DOC)

    inline_roles = [item.role for item in classifications.values()]
    assert "solution_block" in inline_roles
    assert parsed.document_type == "questions_with_inline_solutions"
    assert assembled.questions[0]["answer"] == "4"
    assert assembled.questions[0]["solution_steps"]
    assert assembled.questions[1]["solution_steps"] == []


def test_no_question_document_returns_safe_empty_structure():
    _, _, parsed, assembled = _segment(NO_QUESTION_DOC)

    assert parsed.document_type == "no_questions"
    assert parsed.questions == []
    assert assembled.questions == []
    assert "no_questions_detected" in parsed.warnings


def test_feature_flag_enabled_returns_empty_for_no_question_doc(monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEGMENTATION_ENABLED", "1")
    monkeypatch.setenv("DOCUMENT_SEGMENTATION_SHADOW", "1")
    ocr_result = {"text": NO_QUESTION_DOC, "image_map": {}, "method": "marker"}

    result = step2_preprocess(ocr_result)

    assert result == []
    assert ocr_result["no_questions_detected"] is True
    assert ocr_result["segmentation_report"]["document_type"] == "no_questions"


def test_shadow_mode_keeps_legacy_output_but_emits_report(monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEGMENTATION_ENABLED", "0")
    monkeypatch.setenv("DOCUMENT_SEGMENTATION_SHADOW", "1")
    ocr_result = {"text": QUESTIONS_WITH_SOLUTIONS, "image_map": {}, "method": "marker"}

    result = step2_preprocess(ocr_result)

    assert [q["cau_num"] for q in result] == [1, 2]
    assert ocr_result["segmentation_report"]["question_count"] == 2
    assert "legacy_question_count" in ocr_result["segmentation_report"]


def test_bold_markdown_question_headings_split_into_individual_questions(monkeypatch):
    monkeypatch.setenv("DOCUMENT_SEGMENTATION_ENABLED", "1")
    monkeypatch.setenv("DOCUMENT_SEGMENTATION_SHADOW", "1")
    ocr_result = {"text": BOLD_MARKDOWN_HSG_DOC, "image_map": {}, "method": "marker"}

    result = step2_preprocess(ocr_result)

    assert [q["cau_num"] for q in result] == [1, 2]
    assert "ĐỀ THI KHẢO SÁT" not in result[0]["text"]
    assert result[0]["answer"] == "2"
    assert result[1]["answer"] == "4"
    assert result[0]["solution_steps"]
    assert result[1]["solution_steps"]
