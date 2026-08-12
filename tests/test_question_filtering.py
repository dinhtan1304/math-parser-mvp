from app.services.pipeline import (
    _split_question_and_answer_key,
    step2_preprocess,
)


SAMPLE_MARKER_STYLE_OCR = """
### ĐỀ THI KHẢO SÁT HỌC SINH GIỎI VÒNG II

# MÔN TOÁN 7

## Câu 1 (5,0 điểm)
Tính giá trị của biểu thức $A=1+2$.

## Câu 2 (4,0 điểm)
Chứng minh rằng $n(n+1)$ chia hết cho 2.

### Câu 3 (2,0 điểm)
Tính $x+y$.

—— Hết ——
Thí sinh không được sử dụng tài liệu.
Họ và tên thí sinh: .................
1/7

# UBND HUYỆN TAM ĐẢO TRƯỜNG THCS MINH QUANG
### MÔN TOÁN 7 HƯỚNG DẪN CHẨM

### B. ĐÁP ÁN VÀ THANG ĐIỂM

### Câu 1 (5,0 điểm)
Ta có $A=3$. Đáp số: 3

### Câu 2 (4,0 điểm)
Trong hai số liên tiếp có một số chẵn. Vậy đpcm.

### Câu 3 (2,0 điểm)
Đáp số: $x+y$
"""


def test_fuzzy_answer_key_header_handles_marker_markdown_and_ocr_variant():
    question_text, answer_text = _split_question_and_answer_key(SAMPLE_MARKER_STYLE_OCR)

    assert "HƯỚNG DẪN CHẨM" not in question_text
    assert "ĐÁP ÁN VÀ THANG ĐIỂM" in answer_text


def test_step2_preprocess_keeps_only_questions_and_matched_solutions():
    result = step2_preprocess({"text": SAMPLE_MARKER_STYLE_OCR, "image_map": {}})

    assert [q["cau_num"] for q in result] == [1, 2, 3]
    assert len(result) == 3

    for q in result:
        assert "HƯỚNG DẪN CHẨM" not in q["text"]
        assert "Thí sinh không được sử dụng tài liệu" not in q["text"]
        assert q["solution_steps"], f"Câu {q['cau_num']} phải có solution_steps"

    assert result[0]["answer"] == "3"
    assert result[2]["answer"] == "$x+y$"
