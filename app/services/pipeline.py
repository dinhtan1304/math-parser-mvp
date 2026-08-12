"""
Pipeline — tiền xử lý document trước khi Gemini parse.

Các phần còn active:
- step1_5_chunk_and_embed: chunk markdown + embed cho document RAG
- step2_preprocess: split câu hỏi + tìm đáp án bằng regex (không AI) — dùng để
  ước lượng số câu / shadow segmentation trong process_file

step1_ocr (OCR routing) đã gỡ 2026-06-06; step3_classify (Gemini classify-only)
đã gỡ 2026-07-10 — full-doc ai_parser.parse() thay thế cả hai.
"""

import re
import time
import logging
import os
from typing import Optional, Any

from app.services.answer_extractor import AnswerExtractor
from app.services.native_pdf import extract_native_pdf_markdown
from app.services.pdf_detector import analyze_pdf_for_ocr, recommended_ocr_mode
from app.services.question_markers import (
    RE_MARKDOWN_QUESTION_SPLIT,
    RE_QUESTION_SPLIT,
    find_question_markers,
)

logger = logging.getLogger(__name__)

# Pre-compiled regex for question splitting.
# Regex tách câu dùng chung — định nghĩa ở app/services/question_markers.py.
# Giữ bí danh gạch dưới cho các chỗ dùng sẵn có trong file này.
_RE_QUESTION_SPLIT = RE_QUESTION_SPLIT

# Sprint 5.1 — Markers signal start of solution/answer block trong text gốc.
# Cắt question text TẠI điểm xuất hiện đầu tiên của bất kỳ marker nào.
# Pattern cover trường hợp OCR có space dư giữa các ký tự (broken extraction).
_RE_SOLUTION_MARKER = re.compile(
    r"""(?ix)
    (?:^|\n|\.\s|\s{2,})
    (?:
        L\s*ờ\s*i\s+gi\s*ả\s*i
      | H\s*ư\s*ớ\s*ng\s+d\s*ẫ\s*n\s+gi\s*ả\s*i
      | B\s*à\s*i\s+gi\s*ả\s*i
      | Gi\s*ả\s*i\s*[:\.]
      | Đ\s*á\s*p\s+á\s*n\s*[:\.]
      | ĐÁP\s+ÁN\s*[:\.]
      | Ch\s*ọ\s*n\s+(?:đáp\s+án\s+)?[A-D](?:[\.\s,;:)]|$)
      | K\s*ế\s*t\s+qu\s*ả\s*[:\.]
    )
    """,
)

# Detect "Chọn X" / "Đáp án X" để extract answer letter từ solution block
_RE_ANSWER_PICK = re.compile(
    r'(?i)(?:Ch\s*ọ\s*n|Đ\s*á\s*p\s+á\s*n|ĐÁP\s+ÁN|chọn\s+đáp\s+án)\s*[:\.]?\s*([A-D])'
)


def _strip_solution_from_question(text: str) -> tuple[str, str | None]:
    """Cắt phần solution/answer ra khỏi question text. Trả về (question, answer or None).

    Backup post-process khi Gemini không chịu tách (vẫn gộp solution vào question).
    """
    if not text:
        return text, None
    m = _RE_SOLUTION_MARKER.search(text)
    if not m:
        return text.strip(), None
    cut_at = m.start()
    question_only = text[:cut_at].rstrip(" .,:;\n\t")
    solution_part = text[cut_at:]
    # Try extract answer letter from solution part
    answer_match = _RE_ANSWER_PICK.search(solution_part)
    answer = answer_match.group(1) if answer_match else None
    return question_only, answer

# Fallback: numbered questions "1. " or "1) ".
# Sprint 6 A3: yêu cầu phía sau là chữ in hoa (Latin hoặc tiếng Việt) để
# tránh match "21 )" / "(21):" trong fraction/formula. Đề thi tiếng Việt
# thường mở đầu bằng động từ Tính/Tìm/Cho/Giải/Chứng minh hoặc danh từ
# Cho/Một/Tam giác → đầu chữ in hoa.
_RE_NUMBERED_SPLIT = re.compile(
    r'(?:^|\n)\s*(\d+)\s*[.)]\s+'
    r'(?=[A-ZĐÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴ])',
)

# Fallback #3 (Sprint 8): relaxed numbered split — KHÔNG yêu cầu chữ in hoa sau
# số. Bắt được "1. tính x" / "2) cho tam giác" (đề mở đầu bằng động từ thường)
# mà `_RE_NUMBERED_SPLIT` bỏ sót → tránh gộp nhầm cả đề vào 1 câu. Chỉ dùng khi
# 2 pattern chặt hơn đều thất bại (last resort); vẫn yêu cầu đầu dòng để giảm
# match nhầm số trong phân số/công thức.
_RE_NUMBERED_SPLIT_RELAXED = re.compile(r'(?:^|\n)\s*(\d+)\s*[.)]\s+(?=\S)')

# Bộ nhận diện ranh giới câu nay ở app/services/question_markers.py — dùng
# chung với docling_chunker. Trước đây docling_chunker phải import ngược lên
# module này để lấy `_find_question_markers`, tạo vòng import.
_RE_MARKDOWN_QUESTION_SPLIT = RE_MARKDOWN_QUESTION_SPLIT
_find_question_markers = find_question_markers

# Sprint 6 A2 — Answer key section header.
# Đề thi HSG/Olympiad thường có 2 phần: ĐỀ THI (trang đầu) + HƯỚNG DẪN CHẤM
# (trang cuối). Pipeline phải tách 2 phần để không tạo Câu N duplicate
# (Câu N đề + Câu N đáp án có cùng marker).
_RE_ANSWER_KEY_HEADER = re.compile(
    r'(?im)^\s*(?:'
    r'HƯỚNG\s+DẪN\s+CHẤM'
    r'|ĐÁP\s+ÁN\s+VÀ\s+THANG\s+ĐIỂM'
    r'|ĐÁP\s+ÁN\s+CHI\s+TIẾT'
    r'|BÀI\s+GIẢI\s+THAM\s+KHẢO'
    r'|BARÊM\s+ĐIỂM'
    r'|ĐÁP\s+ÁN(?:\s|$)'
    r'|ĐÁP\s+SỐ'
    r')\s*$'
)


_RE_ANSWER_KEY_HEADER_FUZZY = re.compile(
    r"""(?imx)
    ^\s*
    (?:\#{1,6}\s*)?
    (?:[^\n]*?\s+)?
    (?:
        HƯỚNG\s+DẪN\s+CH(?:Ấ|Ẩ)M
      | ĐÁP\s+ÁN\s+VÀ\s+THANG\s+ĐIỂM
      | ĐÁP\s+ÁN\s+CHI\s+TIẾT
      | BÀI\s+GIẢI\s+THAM\s+KHẢO
      | BARÊM\s+ĐIỂM
      | ĐÁP\s+ÁN
      | ĐÁP\s+SỐ
    )
    [^\n]*$
    """
)

_RE_QUESTION_TRAILER = re.compile(
    r"""(?imx)
    ^\s*(?:
        [—\-=_\s]*Hết[—\-=_\s]*
      | Thí\s+sinh\s+không\s+được\s+sử\s+dụng\s+tài\s+liệu
      | Họ\s+và\s+tên\s+thí\s+sinh
      | Số\s+báo\s+danh
      | \d+\s*/\s*\d+
    ).*$
    """
)


def _split_question_and_answer_key(text: str) -> tuple[str, str]:
    """Sprint 6 A2 — tách (question_section, answer_key_section).

    Detect markers như "HƯỚNG DẪN CHẤM", "ĐÁP ÁN VÀ THANG ĐIỂM" ở đầu dòng
    riêng. Nếu tìm thấy → text trước = đề, text sau = answer key.
    Nếu không có marker → trả (text, "") — coi tất cả là questions.
    """
    if not text:
        return text or "", ""
    m = _RE_ANSWER_KEY_HEADER.search(text) or _RE_ANSWER_KEY_HEADER_FUZZY.search(text)
    if not m:
        return text, ""
    return text[:m.start()].rstrip(), text[m.start():].lstrip()


def _clean_question_block(text: str) -> str:
    """Remove exam boilerplate that often trails the final question block."""
    if not text:
        return text or ""
    trailer = _RE_QUESTION_TRAILER.search(text)
    if trailer:
        text = text[:trailer.start()].rstrip()
    return text.strip()


def _parse_answer_key_by_cau_num(answer_text: str) -> dict[int, str]:
    """Parse answer key text → {cau_num: solution_text}.

    Dùng `_RE_QUESTION_SPLIT` để split answer key theo "Câu N" markers.
    Mỗi block thuộc về Câu N gần nhất.
    """
    if not answer_text.strip():
        return {}
    matches = _find_question_markers(answer_text)
    if not matches:
        return {}
    result: dict[int, str] = {}
    for i, m in enumerate(matches):
        cau_num = int(m.group(1))
        start = m.end()  # bỏ qua "Câu N", lấy nội dung sau đó
        end = matches[i + 1].start() if i + 1 < len(matches) else len(answer_text)
        # Strip leading punctuation (`.`, `:`, `)`) + whitespace từ "Câu N. ..." marker
        block = answer_text[start:end].lstrip(" .:)\t\n").strip()
        if block:
            # Nếu cùng cau_num xuất hiện 2 lần (hiếm), nối thêm
            if cau_num in result:
                result[cau_num] = result[cau_num] + "\n\n" + block
            else:
                result[cau_num] = block
    return result


def _solution_to_steps(solution_text: str, max_steps: int = 30) -> list[str]:
    """Split solution text thành list bước giải.

    Heuristic: split theo blank line hoặc dòng bắt đầu bằng "•", "-", "+",
    "a)", "b)", "Bước N:", "Vậy", v.v. Mỗi step ngắn gọn để FE render đẹp.
    """
    if not solution_text.strip():
        return []
    # Normalize newlines, split theo paragraph
    paragraphs = re.split(r'\n\s*\n', solution_text.strip())
    steps: list[str] = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        # Nếu paragraph dài, split tiếp theo dấu chấm câu nếu cần
        if len(p) > 500:
            # split theo lines còn giữ ngữ cảnh
            for line in p.split("\n"):
                line = line.strip()
                if line:
                    steps.append(line)
        else:
            steps.append(p)
        if len(steps) >= max_steps:
            break
    return steps[:max_steps]


def _extract_final_answer_from_solution(solution_text: str) -> str | None:
    """Tìm đáp án cuối câu trong solution. Heuristic: dòng có "Vậy",
    "Đáp số:", "Kết quả:", hoặc "Chọn X" cho câu trắc nghiệm.
    """
    if not solution_text:
        return None
    # Trắc nghiệm: "Chọn A/B/C/D"
    pick = _RE_ANSWER_PICK.search(solution_text)
    if pick:
        return pick.group(1)
    # Tự luận: "Vậy ... = X" hoặc "Đáp số: X"
    final_patterns = [
        r'Đ\s*á\s*p\s+s\s*ố\s*[:\.]?\s*([^\n]{1,100})',
        r'K\s*ế\s*t\s+qu\s*ả\s*[:\.]?\s*([^\n]{1,100})',
        r'V\s*ậ\s*y\s+[^\n]*?=\s*([^\n.]{1,80})',
    ]
    for pat in final_patterns:
        m = re.search(pat, solution_text, re.IGNORECASE)
        if m:
            return m.group(1).strip().rstrip('.,;:')
    return None

# ==================== STEP 1.5: CHUNK + EMBED (Document RAG) ====================

async def step1_5_chunk_and_embed(
    db,
    file_path: str,
    exam_id: int,
    user_id: int,
    subject_code: str = None,
    grade: int = None,
    markdown_override: str | None = None,
) -> int:
    """Step 1.5: question-aware chunk + embed into pgvector.

    Runs AFTER step 1 (OCR) and IN PARALLEL with step 2/3 (parse questions).
    Chunks the OCR markdown by question boundary (preserves each Câu/Bài, never
    splits a formula). ``markdown_override`` (set on the OCR-review resume) chunks
    the user-EDITED markdown and re-indexes (deletes existing chunks first).

    Returns:
        Number of chunks embedded.
    """
    start = time.time()
    chunks = []

    # Re-index path: chunk the edited markdown, replacing the original chunks.
    if markdown_override is not None:
        try:
            from app.services.docling_chunker import chunk_exam_markdown
            from app.services.document_rag import delete_document_chunks

            removed = await delete_document_chunks(db, exam_id)
            chunks = await chunk_exam_markdown(markdown_override)
            logger.info("Step 1.5 re-index: removed %d old chunks, %d new from edited markdown", removed, len(chunks))
        except Exception as e:
            logger.warning(f"Step 1.5: re-index chunking failed: {e}")
    # Fresh path: prefer cached local OCR markdown (avoids re-OCR).
    else:
        try:
            from app.services.docling_chunker import chunk_exam_markdown
            from app.services.local_ocr_service import extract_local_ocr_artifact

            artifact = await extract_local_ocr_artifact(
                file_path,
                subject_code,
                use_cache=True,
            )
            md_text = artifact.get("markdown") or artifact.get("text") or ""
            if artifact.get("cache_hit") and md_text:
                chunks = await chunk_exam_markdown(md_text)
                logger.info(f"Step 1.5: OCR artifact markdown produced {len(chunks)} chunks")
        except Exception as e:
            logger.warning(f"Step 1.5: OCR artifact chunking failed: {e}")

    # Try Docling next (best chunking quality). Skip on re-index (file-based,
    # would chunk the ORIGINAL PDF instead of the edited markdown).
    try:
        from app.services.docling_chunker import chunk_document, is_available
        if not chunks and markdown_override is None and is_available():
            chunks = await chunk_document(file_path)
            logger.info(f"Step 1.5: Docling produced {len(chunks)} chunks")
    except Exception as e:
        logger.warning(f"Step 1.5: Docling chunking failed: {e}")

    # Fallback: chunk from Marker markdown output (if we have it cached)
    if not chunks and markdown_override is None:
        try:
            from app.services.docling_chunker import chunk_markdown_text
            from app.services.marker_ocr import extract_markdown, is_available as marker_avail
            if marker_avail():
                result = await extract_markdown(file_path)
                md_text = result.get("text", "")
                if md_text:
                    chunks = await chunk_markdown_text(md_text)
                    logger.info(f"Step 1.5: Markdown fallback produced {len(chunks)} chunks")
        except Exception as e:
            logger.warning(f"Step 1.5: Markdown chunking fallback failed: {e}")

    if not chunks:
        logger.info("Step 1.5: No chunks to embed")
        return 0

    # Embed and store chunks
    try:
        from app.services.document_rag import embed_document_chunks
        stored = await embed_document_chunks(
            db, exam_id, chunks, user_id,
            subject_code=subject_code,
            grade=grade,
        )
        elapsed = time.time() - start
        logger.info(
            f"Step 1.5 complete: {stored}/{len(chunks)} chunks embedded, "
            f"time={elapsed:.1f}s"
        )
        return stored
    except Exception as e:
        logger.warning(f"Step 1.5: Embedding failed: {e}")
        return 0


# ==================== STEP 2: PRE-PROCESS ====================

def step2_preprocess(ocr_result: dict) -> list[dict]:
    """Entry point for v1 segmentation rollout with legacy-safe fallback."""
    segmentation_enabled = os.getenv("DOCUMENT_SEGMENTATION_ENABLED", "0") in {"1", "true", "True"}
    segmentation_shadow = os.getenv("DOCUMENT_SEGMENTATION_SHADOW", "1") in {"1", "true", "True"}

    segmentation_questions: list[dict] = []
    segmentation_report: dict[str, Any] | None = None
    if segmentation_enabled or segmentation_shadow:
        segmentation_questions, segmentation_report = _run_document_segmentation(ocr_result)
        ocr_result["segmentation_report"] = segmentation_report

    if segmentation_enabled and segmentation_report is not None:
        if segmentation_report["document_type"] == "no_questions":
            ocr_result["no_questions_detected"] = True
            return []
        if segmentation_questions:
            return _attach_ocr_metadata_to_questions(segmentation_questions, ocr_result)
        segmentation_report["warnings"].append("segmentation_empty_fallback_to_legacy")

    legacy = _legacy_step2_preprocess(ocr_result)
    if segmentation_report is not None:
        segmentation_report["legacy_question_count"] = len(legacy)
        segmentation_report["question_count_delta_vs_legacy"] = (
            segmentation_report["question_count"] - len(legacy)
        )
    return _attach_ocr_metadata_to_questions(legacy, ocr_result)


def _attach_ocr_metadata_to_questions(questions: list[dict], ocr_result: dict) -> list[dict]:
    if ocr_result.get("requires_latex_normalization"):
        for question in questions:
            question["needs_latex_normalization"] = True
    return questions


def _run_document_segmentation(ocr_result: dict) -> tuple[list[dict], dict[str, Any]]:
    from app.services.block_classifier import classify_blocks
    from app.services.document_blocks import build_document_pages
    from app.services.document_structure import build_document_structure
    from app.services.question_assembler import assemble_questions

    pages = build_document_pages(ocr_result)
    blocks = [block for page in pages for block in page.blocks]
    classifications = classify_blocks(blocks)
    parsed = build_document_structure(pages, classifications)
    assembly = assemble_questions(
        parsed,
        full_text=ocr_result.get("text", ""),
        image_map=ocr_result.get("image_map", {}),
    )
    warnings = list(parsed.warnings)
    # Phase 3.11: nêu rõ SỐ câu đáp án không khớp (đáp án mất) thay vì chỉ đếm block.
    if assembly.unmatched_answer_nums:
        warnings.append(
            f"unmatched_answer_nums={assembly.unmatched_answer_nums[:20]} "
            f"(đáp án không khớp câu hỏi nào → có thể mất đáp án)"
        )
    if assembly.skipped_empty:
        warnings.append(f"skipped_empty_questions={assembly.skipped_empty}")
    report = {
        "document_type": parsed.document_type,
        "question_count": len(assembly.questions),
        "section_count": parsed.section_count,
        "warnings": warnings,
        "confidence": parsed.confidence,
        "unmatched_answer_blocks": assembly.unmatched_answer_blocks,
        "unmatched_answer_nums": assembly.unmatched_answer_nums,
        "unmatched_solution_blocks": assembly.unmatched_solution_blocks,
        "skipped_empty": assembly.skipped_empty,
    }
    return assembly.questions, report


def _legacy_step2_preprocess(ocr_result: dict) -> list[dict]:
    """Step 2: Split questions + find answers using regex. No AI.

    Sprint 6 A2: tách "ĐỀ THI" và "HƯỚNG DẪN CHẤM" trước khi splitter chạy.
    Solution từ answer key được gắn vào solution_steps của câu tương ứng.

    Args:
        ocr_result: {text: str, image_map: dict, method: str}

    Returns:
        List of {cau_num, text, answer, answer_source, images, solution_steps}
    """
    text = ocr_result.get("text", "")
    image_map = ocr_result.get("image_map", {})

    if not text.strip():
        return []

    start = time.time()

    # Sprint 6 A2: tách phần đề và phần đáp án TRƯỚC khi splitter chạy.
    # Tránh duplicate Câu N (đề) + Câu N (đáp án) bị merge thành 1 hoặc tách thành 2.
    question_text, answer_key_text = _split_question_and_answer_key(text)
    has_answer_key = bool(answer_key_text.strip())
    if has_answer_key:
        logger.info(
            "A2: detected answer key section (%d chars), splitter chỉ chạy trên đề (%d chars)",
            len(answer_key_text), len(question_text),
        )
    answer_key_map = _parse_answer_key_by_cau_num(answer_key_text) if has_answer_key else {}

    # Sub-step 1: Split QUESTION TEXT (không bao gồm answer key) thành câu hỏi
    questions = _split_questions(question_text)

    if not questions:
        # Fallback: nếu split phần đề không ra câu nào (có thể marker không khớp),
        # thử split lại trên text gốc
        logger.warning("No questions found in question section — falling back to full text")
        questions = _split_questions(text)
        if not questions:
            logger.warning("No questions found by regex splitter on full text either")
            return []

    # Sub-step 2: Extract answers (từ original text — answer extractor có thể tìm
    # ở cuối đề hoặc bảng đáp án rời)
    extractor = AnswerExtractor()
    answer_map = extractor.extract(text, questions)

    # Sub-step 3: Assign answers + solution_steps + images to questions
    result = []
    for q in questions:
        cau_num = q["cau_num"]

        # Answer từ AnswerExtractor (regex bảng đáp án)
        answer = None
        answer_source = None
        if answer_map.confidence >= 0.6 and cau_num in answer_map.answers:
            answer = answer_map.answers[cau_num]
            answer_source = answer_map.source

        # Sprint 6 A2: nếu có answer key section, lấy solution_steps + answer cuối câu
        solution_steps: list[str] = []
        ak_solution = answer_key_map.get(cau_num)
        if ak_solution:
            solution_steps = _solution_to_steps(ak_solution)
            # Nếu chưa có answer, thử extract từ solution text
            if not answer:
                final_ans = _extract_final_answer_from_solution(ak_solution)
                if final_ans:
                    answer = final_ans
                    answer_source = "answer_key"

        # Assign images: check if any image placeholder is in question text
        q_images = {}
        for placeholder, img_path in image_map.items():
            if placeholder in q["text"]:
                q_images[placeholder] = img_path

        result.append({
            "cau_num": cau_num,
            "text": q["text"].strip(),
            "answer": answer,
            "answer_source": answer_source,
            "images": q_images,
            "solution_steps": solution_steps,  # Sprint 6 A2: từ answer key
        })

    elapsed = time.time() - start
    has_answers = sum(1 for r in result if r.get("answer"))
    has_solutions = sum(1 for r in result if r.get("solution_steps"))
    logger.info(
        f"Preprocess: {len(result)} questions, {has_answers} with answers "
        f"(source={answer_map.source}, confidence={answer_map.confidence:.2f}), "
        f"{has_solutions} with solution_steps from answer key, "
        f"time={elapsed:.2f}s"
    )

    return result


def _split_questions(text: str) -> list[dict]:
    """Split OCR text into individual questions using regex.

    Tries "Câu X" / "Bài X" / "Question X" first, falls back to "X. " / "X) ".
    """
    # Try structured patterns first
    matches = _find_question_markers(text)

    if len(matches) < 2:
        # Fallback to numbered patterns (uppercase-gated, ít false positive)
        matches = list(_RE_NUMBERED_SPLIT.finditer(text))

    if len(matches) < 2:
        # Fallback #3: relaxed numbered split (động từ thường "1. tính x").
        relaxed = list(_RE_NUMBERED_SPLIT_RELAXED.finditer(text))
        if len(relaxed) >= 2:
            matches = relaxed

    if len(matches) < 2:
        # Can't split — return entire text as one question. Log to surface the
        # silent merge-to-one (đề định dạng lạ → có thể đang gộp nhầm nhiều câu).
        logger.warning(
            "_split_questions: no question markers matched (text_len=%d); "
            "trả về 1 câu gộp — đề có thể dùng định dạng đánh số không chuẩn",
            len(text),
        )
        return [{"cau_num": 1, "text": text.strip()}]

    questions = []
    for i, m in enumerate(matches):
        cau_num = int(m.group(1))
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        q_text = _clean_question_block(text[start:end].strip())

        if q_text:
            questions.append({"cau_num": cau_num, "text": q_text})

    return questions
