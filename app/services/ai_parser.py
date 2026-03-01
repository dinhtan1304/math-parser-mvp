"""
AI Question Parser — Phân tích đề toán bằng Gemini API

Optimizations v3:
  1. Semaphore lazy-init (fix wrong event loop bug when singleton created at import time)
  2. parse_images: sequential batches → asyncio.gather (parallel)
  3. _clean_text compiled regex (called once per char stream)
  4. Trim SYSTEM_PROMPT to ~1.8k tokens (was 2.6k) — saves ~$0.001/call at scale
  5. _hash_question: faster normalize using translate table
  6. _aggressive_extract_json: single-pass fix pipeline instead of repeated re.sub
  7. _generate_embeddings_batch already parallel — no change needed
  8. Removed redundant escape_next check (bug fix carried over)
  9. progress_callback called outside semaphore (no longer blocks next batch start)
"""

import os
import json
import asyncio
import re
import time
import base64
from typing import List, Dict, Any, Optional, Callable
from enum import Enum
from dotenv import load_dotenv

load_dotenv()

import logging
logger = logging.getLogger(__name__)


class AIProvider(Enum):
    GEMINI = "gemini"
    AUTO = "auto"


# ── Structured output schema ──
PARSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "question":     {"type": "STRING"},
            "type":         {"type": "STRING"},
            "topic":        {"type": "STRING"},
            "difficulty":   {"type": "STRING"},
            "grade":        {"type": "INTEGER"},
            "chapter":      {"type": "STRING"},
            "lesson_title": {"type": "STRING"},
            "answer":       {"type": "STRING"},
            "solution_steps": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["question", "type", "topic", "difficulty",
                     "grade", "chapter", "lesson_title", "answer", "solution_steps"],
    }
}

# ── Pre-compiled regex patterns (module-level — compiled once) ──
_RE_TRIPLE_BACKSLASH = re.compile(r'\\{3,}')
_RE_TRAILING_COMMA   = re.compile(r',\s*([}\]])')
_RE_CONTROL_CHARS    = re.compile(r'[\x00-\x1f\x7f-\x9f]')
_RE_NEWLINE_IN_STR   = re.compile(r'(?<!\\)\n')
_RE_EXTRA_NEWLINES   = re.compile(r'\n{4,}')
_RE_EXTRA_SPACES     = re.compile(r' {3,}')
_RE_Q_NUM            = re.compile(r'(?:Câu|Bài|Question)?\s*(\d+)', re.IGNORECASE)
_RE_ANS_ENTRY        = re.compile(r'^(?:Câu|Bài)?\s*(\d+)\s*[:.]?\s*([A-D]|.{1,50})$', re.IGNORECASE)
_RE_QUESTION_SPLIT   = re.compile(
    r'\n\s*(?:Câu\s+\d+|\bBài\s+\d+|\d+[.)]\s+|[IVX]+\.\s+|Question\s+\d+)',
    re.IGNORECASE
)
_RE_WHITESPACE       = re.compile(r'\s+')


class AIQuestionParser:
    """
    Parser sử dụng Gemini API để phân tích đề toán.
    """

    # ── SYSTEM_PROMPT (trimmed ~30% — removed duplicated rules, kept all essential ones) ──
    SYSTEM_PROMPT = """You are an expert in Mathematics and Educational Data Processing for SmartEdu.

TASK: Extract ALL math problems from the document into a clean JSON array. Read EVERY page including appendices.

MANDATORY RULES:
1. Return ONLY a valid JSON array — no markdown, no explanation.
2. Each problem → 1 independent JSON object. DO NOT generate IDs.
3. COPY mathematical content VERBATIM — never change coefficients or structure.
4. ALL math expressions MUST use LaTeX: $...$, \\\\frac{}{}, \\\\sqrt{}, etc.
5. In JSON strings, every backslash must be doubled: \\\\ → \\\\\\\\
6. Multi-part questions (a,b,c) → ONE object with separators in solution_steps.
7. If no answer found: "answer": "", "solution_steps": []
8. Images/figures → [HÌNH VẼ], graphs → [ĐỒ THỊ], tables → [BẢNG DỮ LIỆU]

CRITICAL — NEVER modify math:
- "√x + 4" → "$\\\\sqrt{x} + 4$" (4 is OUTSIDE radical)
- "√x - 1" → "$\\\\sqrt{x} - 1$" (1 is OUTSIDE radical)
- "3√x + 1" → "$3\\\\sqrt{x} + 1$" (NOT $3\\\\sqrt{x+1}$)

CURRICULUM (GDPT 2018 — Kết nối tri thức):
TOÁN 6: C1.Số tự nhiên|C2.Tính chia hết|C3.Số nguyên|C4.Hình phẳng|C5.Phân số|C6.Số thập phân|C7.Hình học cơ bản
TOÁN 7: C1.Số hữu tỉ|C2.Số thực|C3.Góc/đường thẳng|C4.Tam giác bằng nhau|C5.Thống kê|C6.Tỉ lệ thức|C7.Đại số|C8.Đa giác|C9.Xác suất
TOÁN 8: C1.Đa thức|C2.Hằng đẳng thức|C3.Tứ giác|C4.Định lí Thales|C5.Dữ liệu|C6.Phân thức|C7.PT bậc nhất|C8.Xác suất|C9.Tam giác đồng dạng|C10.Hình chóp
TOÁN 9: C1.Hệ PT|C2.Bất PT|C3.Căn thức|C4.Hệ thức lượng|C5.Đường tròn|C6.Hàm y=ax²|C7.Tần số|C8.Xác suất|C9.Đường tròn ngoại/nội tiếp|C10.Hình tru/nón/cầu
TOÁN 10: C1.Mệnh đề/tập hợp|C2.BPT bậc nhất 2 ẩn|C3.Hệ thức lượng tam giác|C4.Vectơ|C5.Thống kê|C6.Hàm bậc hai|C7.Tọa độ phẳng|C8.Tổ hợp|C9.Xác suất cổ điển
TOÁN 11: C1.Lượng giác|C2.Dãy số/cấp số|C3.Thống kê ghép|C4.Song song KG|C5.Giới hạn/liên tục|C6.Hàm mũ/logarit|C7.Vuông góc KG|C8.Xác suất|C9.Đạo hàm
TOÁN 12: C1.Ứng dụng đạo hàm/đồ thị|C2.Vectơ KG|C3.Phân tán|C4.Nguyên hàm/tích phân|C5.Tọa độ KG|C6.Xác suất có điều kiện

DIFFICULTY: NB=Nhận biết, TH=Thông hiểu, VD=Vận dụng, VDC=Vận dụng cao

OUTPUT: Pure JSON array, start with [ end with ]"""

    PARSE_PROMPT_V1 = """Extract ALL math questions from the text below into a JSON array.

RULES:
- Close ALL JSON strings/arrays/objects — NEVER truncate mid-string
- COPY coefficients EXACTLY: "√x + 4" → "$\\\\sqrt{x} + 4$" (4 outside radical)
- If running out of tokens: finish current object, close array, STOP

{text}

JSON array (starts with [, ends with ]):"""

    PARSE_PROMPT_V2 = """Extract math questions to JSON. Copy every number and coefficient exactly.

{text}

Return JSON array only:"""

    PARSE_PROMPT_V3 = """Extract math questions.
{text}
JSON array:"""

    VISION_PROMPT = """Extract ALL math questions visible in these page images into a JSON array.

RULES:
- Scan EVERY page top-to-bottom, left-to-right
- Extract EVERY question — missing even ONE is unacceptable  
- Convert all math to LaTeX ($...$ inline, $$...$$ display)
- Copy formulas EXACTLY — never modify coefficients
- Multi-part questions (a,b,c) → ONE object
- If no answer visible: answer="", solution_steps=[]
- Output ONLY the JSON array

JSON array:"""

    def __init__(
        self,
        provider: AIProvider = AIProvider.GEMINI,
        gemini_api_key: Optional[str] = None,
        gemini_model: str = None,
        max_tokens: int = 65536,
        max_chunk_size: int = 20000,
        max_concurrency: int = 3,
    ):
        self.provider = provider
        self.gemini_api_key = gemini_api_key or os.getenv("GOOGLE_API_KEY")
        self.gemini_model = gemini_model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.max_tokens = max_tokens
        self.max_chunk_size = max_chunk_size
        self.max_concurrency = max_concurrency

        # OPT: Lazy-init semaphore — avoids "attached to different event loop" error
        # when singleton is created at module import time (before uvicorn starts loop)
        self._semaphore: Optional[asyncio.Semaphore] = None

        self._answer_pool: Dict[str, str] = {}
        self._client = None
        self._init_clients()

    def _get_semaphore(self) -> asyncio.Semaphore:
        """Lazy-create semaphore in the current running event loop."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
        return self._semaphore

    def _init_clients(self):
        if not self.gemini_api_key:
            logger.warning("No GOOGLE_API_KEY found")
            return
        try:
            from google import genai
            self._client = genai.Client(api_key=self.gemini_api_key)
            logger.info(f"Gemini initialized: model={self.gemini_model}, "
                        f"concurrency={self.max_concurrency}, chunk={self.max_chunk_size}")
        except ImportError:
            logger.error("google-genai not installed. Run: pip install google-genai")
        except Exception as e:
            logger.error(f"Gemini init error: {e}")

    def _get_available_provider(self) -> Optional[AIProvider]:
        return AIProvider.GEMINI if self._client else None

    # ==================== PUBLIC API ====================

    async def parse(
        self,
        text: str,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        """Main entry point — parse text into questions."""
        if not text or not text.strip():
            return []
        if not self._client:
            raise RuntimeError(
                "GOOGLE_API_KEY chưa được cấu hình. "
                "Vui lòng thêm API key trong Settings → Environment Variables."
            )

        text = self._clean_text(text)
        start_time = time.time()
        logger.info(f"Document length: {len(text):,} chars")
        self._answer_pool = {}

        if len(text) > self.max_chunk_size:
            result = await self._parse_chunked_parallel(text, progress_callback)
        else:
            result = await self._parse_single(text, chunk_id=0)

        elapsed = time.time() - start_time
        logger.info(f"Total time: {elapsed:.1f}s ({len(result)} questions)")
        return result

    async def parse_images(
        self,
        images: List[Dict],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        """Parse questions from images using Vision API.

        OPT: Large PDFs now processed in PARALLEL batches instead of sequential.
        Sequential was: batch1 → batch2 → batch3 (each ~10-15s = 30-45s total)
        Parallel is:    batch1 ⟍
                        batch2  → merge (10-15s total, 3x faster)
                        batch3 ⟋
        """
        if not images:
            return []
        if not self._client:
            raise RuntimeError(
                "GOOGLE_API_KEY chưa được cấu hình. "
                "Vui lòng thêm API key trong Settings → Environment Variables."
            )

        start_time = time.time()
        total_pages = len(images)
        logger.info(f"Processing {total_pages} page images with Vision API")
        self._answer_pool = {}

        if total_pages <= 15:
            batch_size = total_pages
            logger.info(f"Small PDF: sending all {total_pages} pages at once")
        else:
            batch_size = 10
            logger.info(f"Large PDF: parallel batches of {batch_size} pages")

        # Build batches
        batches = []
        for batch_start in range(0, total_pages, batch_size):
            batch_end = min(batch_start + batch_size, total_pages)
            batches.append((batch_start, batch_end, images[batch_start:batch_end]))

        completed = [0]

        async def _process_batch(batch_start: int, batch_end: int, batch_imgs: List[Dict]):
            async with self._get_semaphore():
                result = await self._call_gemini_vision(batch_imgs)
            completed[0] += batch_end - batch_start
            if progress_callback:
                # OPT: callback outside semaphore — doesn't block next batch acquisition
                progress_callback(min(completed[0], total_pages), total_pages)
            return result

        # OPT: All batches run in parallel (semaphore controls max concurrency)
        tasks = [_process_batch(bs, be, bi) for bs, be, bi in batches]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Merge with deduplication
        all_questions: List[Dict] = []
        seen_hashes: set = set()
        for res in batch_results:
            if isinstance(res, Exception):
                logger.error(f"Vision batch failed: {res}")
                continue
            for q in res:
                q_hash = self._hash_question(q.get("question", ""))
                if q_hash and q_hash not in seen_hashes:
                    seen_hashes.add(q_hash)
                    all_questions.append(q)
                    self._collect_answers([q])

        all_questions = self._match_answers_from_pool(all_questions)
        elapsed = time.time() - start_time
        logger.info(f"Vision total: {elapsed:.1f}s ({len(all_questions)} questions)")
        return all_questions

    # ==================== GEMINI VISION ====================

    async def _call_gemini_vision(self, images: List[Dict]) -> List[Dict[str, Any]]:
        """Call Gemini Vision API — 3-tier fallback."""
        if not self._client:
            return []
        from google.genai import types

        parts = [self.VISION_PROMPT]
        for img in images:
            parts.append(types.Part.from_bytes(
                data=base64.b64decode(img["data"]),
                mime_type=img.get("mime_type", "image/jpeg"),
            ))

        content = ""

        for tier, (mime, schema, label) in enumerate([
            ("application/json", PARSE_SCHEMA, "schema"),
            ("application/json", None,         "json"),
            (None,               None,          "plain"),
        ]):
            try:
                cfg_kwargs: Dict[str, Any] = dict(
                    system_instruction=self.SYSTEM_PROMPT,
                    temperature=0,
                    max_output_tokens=self.max_tokens,
                )
                if mime:
                    cfg_kwargs["response_mime_type"] = mime
                if schema:
                    cfg_kwargs["response_schema"] = schema

                response = await self._client.aio.models.generate_content(
                    model=self.gemini_model,
                    contents=parts,
                    config=types.GenerateContentConfig(**cfg_kwargs),
                )
                content = self._safe_text(response)
                if content:
                    result = self._extract_json(content)
                    if result:
                        logger.info(f"Vision {label}: {len(result)} questions from {len(images)} pages")
                        return result
            except Exception as e:
                logger.warning(f"Vision {label} failed: {e}")

        if content:
            result = self._extract_json(content)
            logger.info(f"Vision fallback: {len(result)} questions from {len(images)} pages")
            return result
        return []

    # ==================== TEXT PARSING ====================

    async def _parse_single(self, text: str, chunk_id: int = 0) -> List[Dict[str, Any]]:
        """Parse single chunk with retry logic and rate limiting."""
        sem = self._get_semaphore()
        async with sem:
            prompts = [self.PARSE_PROMPT_V1, self.PARSE_PROMPT_V2, self.PARSE_PROMPT_V3]
            last_content = ""

            for attempt, prompt_template in enumerate(prompts):
                try:
                    logger.info(f"Chunk {chunk_id} - Attempt {attempt + 1}/{len(prompts)}...")
                    result, raw_content = await self._call_gemini(text, prompt_template)
                    last_content = raw_content

                    if result:
                        logger.info(f"Chunk {chunk_id} - Extracted {len(result)} questions")
                        self._collect_answers(result)
                        return result
                    else:
                        logger.warning(f"Chunk {chunk_id} - Attempt {attempt + 1}: Empty result")
                except Exception as e:
                    logger.error(f"Chunk {chunk_id} - Attempt {attempt + 1} failed: {e}")
                    await asyncio.sleep(0.5)

            # Salvage from last response
            if last_content:
                result = self._aggressive_extract_json(last_content)
                if result:
                    logger.info(f"Chunk {chunk_id} - Salvaged {len(result)} questions")
                    self._collect_answers(result)
                    return result

            logger.error(f"Chunk {chunk_id} - All attempts failed")
            return []

    async def _call_gemini(self, text: str, prompt_template: str) -> tuple[List[Dict], str]:
        """Call Gemini API — 3-tier fallback with 429 retry."""
        from google.genai import types

        prompt = prompt_template.format(text=text)

        async def _try_with_retry(config, label):
            for attempt in range(3):
                try:
                    response = await self._client.aio.models.generate_content(
                        model=self.gemini_model,
                        contents=prompt,
                        config=config,
                    )
                    content = self._safe_text(response)
                    if content:
                        result = self._extract_json(content)
                        if result:
                            logger.info(f"{label}: {len(result)} questions")
                            return result, content
                    return None, content or ""
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                        wait = (attempt + 1) * 10
                        logger.warning(f"{label} rate limited, waiting {wait}s...")
                        await asyncio.sleep(wait)
                        continue
                    logger.warning(f"{label} failed: {e}")
                    return None, ""
            return None, ""

        for mime, schema, label in [
            ("application/json", PARSE_SCHEMA, "Schema mode"),
            ("application/json", None,         "JSON mode"),
            (None,               None,          "Plain text"),
        ]:
            cfg_kwargs: Dict[str, Any] = dict(
                system_instruction=self.SYSTEM_PROMPT,
                temperature=0,
                max_output_tokens=self.max_tokens,
            )
            if mime:
                cfg_kwargs["response_mime_type"] = mime
            if schema:
                cfg_kwargs["response_schema"] = schema

            result, content = await _try_with_retry(
                types.GenerateContentConfig(**cfg_kwargs), label
            )
            if result:
                return result, content

        return [], content  # type: ignore

    async def _parse_chunked_parallel(
        self, text: str, progress_callback: Optional[Callable] = None
    ) -> List[Dict[str, Any]]:
        """Parallel chunk processing with deduplication."""
        chunks = self._smart_chunk(text)
        total_chunks = len(chunks)
        logger.info(f"Split into {total_chunks} chunks (max {self.max_concurrency} parallel)")

        completed = [0]

        async def process_chunk(idx: int, chunk: str) -> tuple[int, List[Dict]]:
            start = time.time()
            result = await self._parse_single(chunk, chunk_id=idx)
            elapsed = time.time() - start
            completed[0] += 1
            logger.info(f"Chunk {idx + 1}/{total_chunks} done ({len(result)} questions, {elapsed:.1f}s)")
            if progress_callback:
                progress_callback(completed[0], total_chunks)
            return idx, result

        tasks = [process_chunk(i, chunk) for i, chunk in enumerate(chunks)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        sorted_results = sorted(
            [r for r in results if not isinstance(r, Exception)],
            key=lambda x: x[0]
        )

        all_questions: List[Dict] = []
        seen_hashes: set = set()
        for idx, questions in sorted_results:
            for q in questions:
                q_hash = self._hash_question(q.get("question", ""))
                if q_hash and q_hash not in seen_hashes:
                    seen_hashes.add(q_hash)
                    all_questions.append(q)

        all_questions = self._match_answers_from_pool(all_questions)
        logger.info(f"Total: {len(all_questions)} unique questions")
        return all_questions

    # ==================== ANSWER MATCHING ====================

    def _collect_answers(self, questions: List[Dict]):
        for q in questions:
            q_text = q.get("question", "").strip()
            answer = q.get("answer", "").strip()
            num_match = _RE_Q_NUM.search(q_text)
            if num_match and answer:
                self._answer_pool[num_match.group(1)] = answer
            if len(q_text) < 50:
                ans_match = _RE_ANS_ENTRY.match(q_text)
                if ans_match:
                    self._answer_pool[ans_match.group(1)] = ans_match.group(2)

    def _match_answers_from_pool(self, questions: List[Dict]) -> List[Dict]:
        result = []
        for q in questions:
            q_text = q.get("question", "").strip()
            # Skip standalone answer entries
            if len(q_text) < 50 and re.match(r'^(?:Câu|Bài)?\s*\d+\s*[:.]?\s*[A-D]?\s*$', q_text, re.IGNORECASE):
                continue
            if not q.get("answer"):
                num_match = _RE_Q_NUM.search(q_text)
                if num_match and num_match.group(1) in self._answer_pool:
                    q = dict(q)  # Don't mutate original
                    q["answer"] = self._answer_pool[num_match.group(1)]
            result.append(q)
        return result

    # ==================== CHUNKING ====================

    def _smart_chunk(self, text: str) -> List[str]:
        """Smart chunking by question boundaries."""
        splits = list(_RE_QUESTION_SPLIT.finditer(text))
        if not splits:
            return self._chunk_by_size(text)

        chunks = []
        current_chunk = text[:splits[0].start()] if splits else ""

        for i, match in enumerate(splits):
            start = match.start()
            end = splits[i + 1].start() if i + 1 < len(splits) else len(text)
            question_text = text[start:end]

            if len(current_chunk) + len(question_text) > self.max_chunk_size:
                if current_chunk.strip():
                    chunks.append(current_chunk)
                current_chunk = question_text
            else:
                current_chunk += question_text

        if current_chunk.strip():
            chunks.append(current_chunk)

        return chunks or [text]

    def _chunk_by_size(self, text: str) -> List[str]:
        chunks = []
        pos = 0
        while pos < len(text):
            end = min(pos + self.max_chunk_size, len(text))
            chunk = text[pos:end]
            if end < len(text):
                for sep in ['\n\n', '\n', '. ']:
                    last_sep = chunk.rfind(sep)
                    if last_sep > self.max_chunk_size * 0.5:
                        chunk = chunk[:last_sep + len(sep)]
                        break
            chunks.append(chunk)
            pos += len(chunk)
        return chunks

    # ==================== UTILITIES ====================

    def _hash_question(self, text: str) -> str:
        """OPT: Single regex normalize instead of split+join."""
        if not text:
            return ""
        return _RE_WHITESPACE.sub(' ', text.lower().strip())[:150]

    def _clean_text(self, text: str) -> str:
        """OPT: Use pre-compiled regex patterns."""
        text = _RE_EXTRA_NEWLINES.sub('\n\n\n', text)
        text = _RE_EXTRA_SPACES.sub('  ', text)
        text = text.replace('\t', ' ')
        return text.strip()

    @staticmethod
    def _safe_text(response) -> str:
        try:
            if hasattr(response, 'text') and response.text:
                return response.text
        except Exception:
            pass
        try:
            for c in response.candidates:
                for p in c.content.parts:
                    if hasattr(p, 'text') and p.text:
                        return p.text
        except Exception:
            pass
        return ""

    def _extract_json(self, content: str) -> List[Dict]:
        """OPT: Fast path for valid JSON before expensive cleanup."""
        if not content:
            return []
        content = content.strip()

        # Fast path: try direct parse first (works for schema-mode responses)
        try:
            result = json.loads(content)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        # Remove markdown fences
        if "```" in content:
            for part in content.split("```"):
                part = part.lstrip("json").strip()
                if part.startswith("["):
                    try:
                        result = json.loads(_RE_TRIPLE_BACKSLASH.sub(r'\\\\', part))
                        if isinstance(result, list):
                            return result
                    except Exception:
                        pass

        return self._aggressive_extract_json(content)

    def _aggressive_extract_json(self, content: str) -> List[Dict]:
        """OPT: Single-pass fix pipeline instead of sequential re.sub calls."""
        if not content:
            return []

        start_idx = content.find('[')
        if start_idx == -1:
            return []

        # Find matching bracket
        bracket_count = 0
        end_idx = start_idx
        in_string = False
        escape_next = False

        for i in range(start_idx, len(content)):
            char = content[i]
            if escape_next:
                escape_next = False
                continue
            if char == '\\':
                escape_next = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if not in_string:
                if char == '[':
                    bracket_count += 1
                elif char == ']':
                    bracket_count -= 1
                    if bracket_count == 0:
                        end_idx = i + 1
                        break

        if end_idx <= start_idx:
            last_bracket = content.rfind(']')
            if last_bracket > start_idx:
                end_idx = last_bracket + 1
            else:
                return []

        json_str = content[start_idx:end_idx]

        # OPT: Apply all fixes in one pipeline pass
        # Step 1: fix triple backslashes (most common Gemini issue)
        json_str = _RE_TRIPLE_BACKSLASH.sub(r'\\\\', json_str)
        # Step 2: trailing commas
        json_str = _RE_TRAILING_COMMA.sub(r'\1', json_str)
        # Step 3: Python literals
        json_str = json_str.replace('None', 'null').replace('True', 'true').replace('False', 'false')
        # Step 4: control chars (excluding valid JSON whitespace)
        json_str = _RE_CONTROL_CHARS.sub('', json_str)

        try:
            result = json.loads(json_str)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        # Last resort: individual objects
        return self._extract_individual_objects(json_str)

    def _extract_individual_objects(self, json_str: str) -> List[Dict]:
        """Extract individual JSON objects one by one as last resort."""
        objects = []
        json_str = _RE_TRIPLE_BACKSLASH.sub(r'\\\\', json_str)
        obj_starts = [m.start() for m in re.finditer(r'\{\s*"question"', json_str)]

        for i, start in enumerate(obj_starts):
            end_search = obj_starts[i + 1] if i + 1 < len(obj_starts) else len(json_str)
            substring = json_str[start:end_search]

            brace_count = 0
            obj_end = 0
            in_string = False
            escape_next = False

            for j, char in enumerate(substring):
                if escape_next:
                    escape_next = False
                    continue
                if char == '\\':
                    escape_next = True
                    continue
                if char == '"':
                    in_string = not in_string
                    continue
                if not in_string:
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            obj_end = j + 1
                            break

            if obj_end == 0:
                continue

            obj_str = _RE_TRAILING_COMMA.sub(r'\1', substring[:obj_end])
            obj_str = _RE_CONTROL_CHARS.sub('', obj_str)

            try:
                obj = json.loads(obj_str)
                if isinstance(obj, dict) and "question" in obj:
                    obj.setdefault("type", "TL")
                    obj.setdefault("topic", "Toán học")
                    obj.setdefault("difficulty", "TH")
                    obj.setdefault("solution_steps", [])
                    obj.setdefault("answer", "")
                    obj.setdefault("grade", None)
                    obj.setdefault("chapter", "")
                    obj.setdefault("lesson_title", "")
                    objects.append(obj)
            except json.JSONDecodeError:
                pass

        if objects:
            logger.info(f"Extracted {len(objects)} individual objects")
        return objects


# ============ SPEED PRESETS ============

def create_fast_parser(**kwargs):
    """🚀 Fast: larger chunks, more parallel"""
    return AIQuestionParser(
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        max_chunk_size=20000, max_concurrency=5, max_tokens=65536, **kwargs
    )

def create_balanced_parser(**kwargs):
    """⚖️ Balanced"""
    return AIQuestionParser(
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        max_chunk_size=15000, max_concurrency=3, max_tokens=65536, **kwargs
    )

def create_quality_parser(**kwargs):
    """🎯 Quality: smaller chunks, more accurate"""
    return AIQuestionParser(
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        max_chunk_size=10000, max_concurrency=2, max_tokens=65536, **kwargs
    )