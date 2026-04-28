"""
Pipeline — OCR-first processing pipeline.

3 bước:
1. step1_ocr: Chọn OCR backend theo môn, extract text + images
2. step2_preprocess: Split câu hỏi + tìm đáp án bằng regex (không AI)
3. step3_classify: Gửi structured JSON cho Gemini classify only

Gemini KHÔNG extract text — chỉ classify type/difficulty/topic.
Giảm token ~50-60%, thời gian ~60%.
"""

import re
import json
import time
import asyncio
import logging
from typing import Optional, Callable, Any

from app.services.ocr_router import get_ocr_config, OCRBackend
from app.services.answer_extractor import AnswerExtractor
from app.services.subject_prompts import SUBJECT_TO_FAMILY

logger = logging.getLogger(__name__)

# Pre-compiled regex for question splitting
_RE_QUESTION_SPLIT = re.compile(
    r'(?:^|\n)\s*(?:Câu|câu|Bài|bài|Question)\s+(\d+)\s*[.:\)]',
    re.IGNORECASE
)

# Fallback: numbered questions "1. " or "1) "
_RE_NUMBERED_SPLIT = re.compile(
    r'(?:^|\n)\s*(\d+)\s*[.)]\s+',
)


# ==================== STEP 1: OCR ====================

async def step1_ocr(file_path: str, subject_code: str) -> dict:
    """Step 1: OCR — chọn backend theo môn học, extract text + images.

    Returns:
        {text: str, image_map: dict, method: str}
    """
    from app.services import file_handler

    ocr_config = get_ocr_config(subject_code)
    backend = ocr_config.backend
    logger.info(
        f"OCR routing: subject={subject_code} → backend={backend.value} | {ocr_config.reason}"
    )

    start = time.time()
    result = None

    # Route to the right OCR backend
    if backend == OCRBackend.PYMUPDF:
        result = await _ocr_pymupdf(file_handler, file_path)
    elif backend == OCRBackend.PIX2TEXT:
        result = await _ocr_pix2text(file_handler, file_path)
    elif backend == OCRBackend.MINERU:
        result = await _ocr_mineru(file_handler, file_path)

    if not result or not result.get("text"):
        # result is empty — log and try fallbacks
        attempted = backend.value
        result = await _ocr_with_fallback(file_handler, file_path, attempted)

    # Quality check: if PyMuPDF text is poor, auto-upgrade
    if result.get("method") == "pymupdf" and _is_text_poor_quality(result.get("text", "")):
        logger.info(f"PyMuPDF text quality poor for {subject_code}, upgrading to Pix2Text")
        p2t_result = await _ocr_pix2text(file_handler, file_path)
        if p2t_result and p2t_result.get("text"):
            result = p2t_result

    elapsed = time.time() - start
    text_len = len(result.get("text", ""))
    img_count = len(result.get("image_map", {}))
    logger.info(
        f"OCR complete: method={result.get('method')}, "
        f"text={text_len} chars, images={img_count}, time={elapsed:.1f}s"
    )

    # Ensure image_map key exists
    if "image_map" not in result:
        result["image_map"] = {}

    return result


async def _ocr_pymupdf(fh: Any, file_path: str) -> dict:
    """OCR via PyMuPDF (fast text extraction)."""
    try:
        extracted = await fh.extract_text(file_path, use_vision=False)
        return {
            "text": extracted.get("text", ""),
            "image_map": {},
            "method": "pymupdf",
        }
    except Exception as e:
        logger.warning(f"PyMuPDF extraction failed: {e}")
        return {"text": "", "image_map": {}, "method": "pymupdf-error"}


async def _ocr_pix2text(fh: Any, file_path: str) -> dict:
    """OCR via Pix2Text (local OCR + LaTeX formula recognition)."""
    if not fh.has_pix2text:
        logger.warning("Pix2Text not installed, cannot use pix2text backend")
        return {"text": "", "image_map": {}, "method": "pix2text-not-installed"}

    try:
        extracted = await fh._extract_pdf_pix2text(file_path)
        return {
            "text": extracted.get("text", ""),
            "image_map": {},
            "method": "pix2text",
        }
    except Exception as e:
        logger.warning(f"Pix2Text extraction failed: {e}")
        return {"text": "", "image_map": {}, "method": "pix2text-error"}


async def _ocr_mineru(fh: Any, file_path: str) -> dict:
    """OCR via MinerU (layout-aware extraction)."""
    try:
        result = await fh._extract_pdf_mineru(file_path)
        return result  # Already has text, image_map, method
    except Exception as e:
        logger.warning(f"MinerU extraction failed: {e}")
        return {"text": "", "image_map": {}, "method": "mineru-error"}


async def _ocr_with_fallback(fh: Any, file_path: str, attempted: str) -> dict:
    """Fallback chain: MinerU → Pix2Text → PyMuPDF."""
    logger.warning(f"Primary OCR ({attempted}) failed/empty, trying fallbacks...")

    if attempted != "pix2text" and fh.has_pix2text:
        result = await _ocr_pix2text(fh, file_path)
        if result.get("text"):
            logger.info("Fallback to Pix2Text succeeded")
            return result

    if attempted != "pymupdf":
        result = await _ocr_pymupdf(fh, file_path)
        if result.get("text"):
            logger.info("Fallback to PyMuPDF succeeded")
            return result

    logger.error("All OCR backends failed")
    return {"text": "", "image_map": {}, "method": "all-failed"}


def _is_text_poor_quality(text: str) -> bool:
    """Quick heuristic: check if extracted text is garbage."""
    if not text or len(text) < 50:
        return True
    doc_markers = ['Câu', 'Bài', 'câu', 'bài', 'Question', '=', '+', 'sin', 'cos']
    marker_count = sum(1 for m in doc_markers if m in text)
    if marker_count < 2:
        return True
    garbled = sum(1 for c in text if ord(c) > 0xFFFF or (ord(c) < 32 and c not in '\n\r\t'))
    if len(text) > 0 and garbled / len(text) > 0.1:
        return True
    return False


# ==================== STEP 2: PRE-PROCESS ====================

def step2_preprocess(ocr_result: dict) -> list[dict]:
    """Step 2: Split questions + find answers using regex. No AI.

    Args:
        ocr_result: {text: str, image_map: dict, method: str}

    Returns:
        List of {cau_num, text, answer, answer_source, images}
    """
    text = ocr_result.get("text", "")
    image_map = ocr_result.get("image_map", {})

    if not text.strip():
        return []

    start = time.time()

    # Sub-step 1: Split text into questions
    questions = _split_questions(text)

    if not questions:
        logger.warning("No questions found by regex splitter")
        return []

    # Sub-step 2: Extract answers
    extractor = AnswerExtractor()
    answer_map = extractor.extract(text, questions)

    # Sub-step 3: Assign answers and images to questions
    result = []
    for q in questions:
        cau_num = q["cau_num"]

        # Assign answer if confidence is sufficient
        answer = None
        answer_source = None
        if answer_map.confidence >= 0.6 and cau_num in answer_map.answers:
            answer = answer_map.answers[cau_num]
            answer_source = answer_map.source

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
        })

    elapsed = time.time() - start
    has_answers = sum(1 for r in result if r.get("answer"))
    logger.info(
        f"Preprocess: {len(result)} questions, {has_answers} with answers "
        f"(source={answer_map.source}, confidence={answer_map.confidence:.2f}), "
        f"time={elapsed:.2f}s"
    )

    return result


def _split_questions(text: str) -> list[dict]:
    """Split OCR text into individual questions using regex.

    Tries "Câu X" / "Bài X" / "Question X" first, falls back to "X. " / "X) ".
    """
    # Try structured patterns first
    matches = list(_RE_QUESTION_SPLIT.finditer(text))

    if len(matches) < 2:
        # Fallback to numbered patterns
        matches = list(_RE_NUMBERED_SPLIT.finditer(text))

    if len(matches) < 2:
        # Can't split — return entire text as one question
        return [{"cau_num": 1, "text": text.strip()}]

    questions = []
    for i, m in enumerate(matches):
        cau_num = int(m.group(1))
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        q_text = text[start:end].strip()

        if q_text:
            questions.append({"cau_num": cau_num, "text": q_text})

    return questions


# ==================== STEP 3: GEMINI CLASSIFY ====================

# Classify prompt template — Gemini CHỈ classify, KHÔNG extract text
_CLASSIFY_PROMPT = """Bạn là chuyên gia phân loại câu hỏi đề thi K12 Việt Nam, môn {subject_family}.
Các câu hỏi dưới đây đã được OCR và trích xuất sẵn. Đáp án (nếu có) đã được điền.

Nhiệm vụ của bạn: CHỈ classify và điền thông tin còn thiếu.
KHÔNG thay đổi nội dung câu hỏi. KHÔNG bịa solution_steps.

Với mỗi câu, trả về JSON object:
{{
  "cau_num": số nguyên (giữ nguyên từ input),
  "type": "TN"|"TL"|"Chứng minh"|"Tính toán"|"Tìm x"|"Rút gọn biểu thức"|"Đọc hiểu"|"Nghị luận"|"Tập làm văn",
  "difficulty": "NB"|"TH"|"VD"|"VDC",
  "topic": chủ đề ngắn (ví dụ: "Giới hạn và liên tục", "Dao động cơ", "Kim loại kiềm"),
  "grade": lớp học (số nguyên 1-12, null nếu không rõ),
  "answer": giữ nguyên nếu đã có, tự điền nếu TN và đáp án rõ ràng từ text, null nếu TL không có đáp án,
  "solution_steps": [các bước giải CHỈ NẾU có trong text, không được bịa, [] nếu không có]
}}

Trả về JSON array. KHÔNG markdown. KHÔNG giải thích.

INPUT:
{questions_json}"""

# Schema for structured output
_CLASSIFY_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "cau_num":        {"type": "INTEGER"},
            "type":           {"type": "STRING"},
            "difficulty":     {"type": "STRING"},
            "topic":          {"type": "STRING"},
            "grade":          {"type": "INTEGER"},
            "answer":         {"type": "STRING"},
            "solution_steps": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["cau_num", "type", "difficulty", "topic"],
    }
}


async def step3_classify(
    structured_questions: list[dict],
    subject_hint: Optional[str] = None,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> list[dict]:
    """Step 3: Send structured questions to Gemini for classification only.

    Chunks of 10 questions, max 4 parallel.
    Returns final list with question text + classification merged.
    """
    from app.services import ai_parser_service as ai_parser

    if not ai_parser._client:
        raise RuntimeError(
            "GOOGLE_API_KEY chưa được cấu hình. "
            "Vui lòng thêm API key trong Settings → Environment Variables."
        )

    subject_family = SUBJECT_TO_FAMILY.get(subject_hint, "generic")

    start = time.time()
    ai_parser._reset_token_usage()

    # Build chunks of 10
    chunk_size = 10
    chunks = []
    for i in range(0, len(structured_questions), chunk_size):
        chunks.append(structured_questions[i:i + chunk_size])

    total_chunks = len(chunks)
    completed = 0
    semaphore = asyncio.Semaphore(4)

    async def _classify_chunk(chunk: list[dict], chunk_idx: int) -> list[dict]:
        nonlocal completed

        # Build input JSON for Gemini (only send text + answer, not images)
        input_items = []
        for q in chunk:
            item = {
                "cau_num": q["cau_num"],
                "text": q["text"][:3000],  # Truncate very long questions
            }
            if q.get("answer"):
                item["answer"] = q["answer"]
            input_items.append(item)

        questions_json = json.dumps(input_items, ensure_ascii=False, indent=None)
        prompt = _CLASSIFY_PROMPT.format(
            subject_family=subject_family,
            questions_json=questions_json,
        )

        async with semaphore:
            result = await _call_gemini_classify(ai_parser, prompt)

        completed += 1
        if progress_cb:
            progress_cb(completed, total_chunks)

        return result

    # Run all chunks in parallel (semaphore limits to 4)
    tasks = [_classify_chunk(chunk, i) for i, chunk in enumerate(chunks)]
    chunk_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Merge results
    classify_map: dict[int, dict] = {}
    for i, res in enumerate(chunk_results):
        if isinstance(res, Exception):
            logger.warning(f"Classify chunk {i} failed: {res}")
            continue
        for item in res:
            cau_num = item.get("cau_num")
            if cau_num is not None:
                classify_map[cau_num] = item

    # Merge classification back into structured questions
    final_questions = []
    for q in structured_questions:
        cau_num = q["cau_num"]
        classify = classify_map.get(cau_num, {})

        # Build final question dict (compatible with existing Question model fields)
        fq = {
            "question": q["text"],
            "subject": subject_hint or "toan",
            "type": classify.get("type", "TN"),
            "difficulty": classify.get("difficulty", "TH"),
            "topic": classify.get("topic", ""),
            "grade": classify.get("grade"),
            "chapter": "",
            "lesson_title": "",
            "answer": q.get("answer") or classify.get("answer") or "",
            "answer_source": q.get("answer_source") or ("gemini" if classify.get("answer") else None),
            "solution_steps": classify.get("solution_steps", []),
        }

        # Restore answers: if Gemini returned null but preprocess had high-confidence answer
        if not fq["answer"] and q.get("answer"):
            fq["answer"] = q["answer"]
            fq["answer_source"] = q.get("answer_source")

        final_questions.append(fq)

    # Sort by cau_num (preserve original order)
    final_questions.sort(key=lambda x: _extract_cau_num(x.get("question", "")))

    elapsed = time.time() - start
    ai_parser._log_token_summary(f"Classify ({len(final_questions)} questions, {elapsed:.1f}s)")

    logger.info(
        f"Classification complete: {len(final_questions)} questions, "
        f"{len(classify_map)}/{len(structured_questions)} classified by Gemini, "
        f"time={elapsed:.1f}s"
    )

    return final_questions


def _extract_cau_num(text: str) -> int:
    """Extract question number from text for sorting."""
    m = re.search(r'(?:Câu|câu|Bài|bài|Question)\s+(\d+)', text)
    if m:
        return int(m.group(1))
    m = re.search(r'^(\d+)', text)
    if m:
        return int(m.group(1))
    return 999


async def _call_gemini_classify(ai_parser: Any, prompt: str) -> list[dict]:
    """Call Gemini API for classification. 3-tier fallback."""
    from google.genai import types
    from app.services.ai_parser import _SAFETY_SETTINGS

    tiers = [
        ("application/json", _CLASSIFY_SCHEMA, "schema"),
        ("application/json", None,              "json"),
        (None,               None,               "plain"),
    ]

    for mime, schema, label in tiers:
        try:
            cfg_kwargs = dict(
                temperature=0,
                max_output_tokens=8192,
                safety_settings=[types.SafetySetting(**s) for s in _SAFETY_SETTINGS],
            )
            if mime:
                cfg_kwargs["response_mime_type"] = mime
            if schema:
                cfg_kwargs["response_schema"] = schema

            for attempt in range(2):
                try:
                    response = await asyncio.wait_for(
                        ai_parser._client.aio.models.generate_content(
                            model=ai_parser.gemini_model,
                            contents=prompt,
                            config=types.GenerateContentConfig(**cfg_kwargs),
                        ),
                        timeout=90,
                    )
                    ai_parser._track_tokens(response)
                    content = ai_parser._safe_text(response)

                    if content:
                        result = ai_parser._extract_json(content)
                        if result:
                            logger.debug(f"Classify {label}: {len(result)} items")
                            return result
                    break  # Got response but bad JSON → try next tier

                except asyncio.TimeoutError:
                    logger.warning(f"Classify {label} timed out, attempt {attempt + 1}")
                    break
                except Exception as e:
                    err = str(e)
                    if "429" in err or "RESOURCE_EXHAUSTED" in err:
                        wait = (attempt + 1) * 5
                        logger.warning(f"Classify {label} rate limited, wait {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    if "500" in err or "503" in err:
                        wait = (attempt + 1) * 15
                        logger.warning(f"Classify {label} server error, wait {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    logger.warning(f"Classify {label} failed: {e}")
                    break

        except Exception as e:
            logger.warning(f"Classify {label} outer error: {e}")

    logger.warning("All classify tiers failed, returning empty")
    return []
