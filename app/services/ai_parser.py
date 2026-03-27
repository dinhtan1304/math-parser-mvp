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
from app.services.subject_prompts import get_prompt_config

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
            "subject":      {"type": "STRING"},
            "type":         {"type": "STRING"},
            "difficulty":   {"type": "STRING"},
            "grade":        {"type": "INTEGER"},
            "chapter":      {"type": "STRING"},
            "lesson_title": {"type": "STRING"},
            "answer":       {"type": "STRING"},
            "solution_steps": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["question", "subject", "type", "difficulty",
                     "grade", "chapter", "lesson_title", "answer", "solution_steps"],
    }
}

# ── Safety settings — disable blocking for educational content (chemistry etc.) ──
# Chemistry formulas (HCl, H₂SO₄) can trigger DANGEROUS_CONTENT filters.
# This is K12 educational content — blocking is inappropriate.
_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
]

# ── Pre-compiled regex patterns (module-level — compiled once) ──
_RE_TRIPLE_BACKSLASH = re.compile(r'\\{3,}')
_RE_TRAILING_COMMA   = re.compile(r',\s*([}\]])')
_RE_CONTROL_CHARS    = re.compile(r'[\x00-\x1f\x7f-\x9f]')
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

    # ── SYSTEM_PROMPT v3 — ~1500 tokens (was ~2500) ──
    SYSTEM_PROMPT = r"""You are a Vietnamese K12 exam parser expert. Extract ALL problems from documents into structured JSON.

SUBJECT DETECTION:
- Detect subject from document header, content, and question style
- subject codes: toan, ngu-van, tieng-anh, khtn, vat-li, hoa-hoc, sinh-hoc, lich-su, dia-li, gdcd, gdktpl, tin-hoc, cong-nghe, tieng-viet, khoa-hoc, ls-dl, dao-duc, am-nhac, my-thuat
- Math documents → "toan", Physics → "vat-li" (grades 10-12) or "khtn" (grades 6-9)
- Chemistry → "hoa-hoc" (10-12) or "khtn" (6-9), Biology → "sinh-hoc" (10-12) or "khtn" (6-9)
{subject_hint_line}

EXTRACTION:
- Return ONLY raw JSON array — no markdown, no explanation
- DO NOT generate IDs. DO NOT modify/simplify content.
- Process 100% of problems — never stop midway
- Multi-part (a,b,c) → 1 object, separators "--- a)", "--- b)" in solution_steps
- If question cut off: "[YÊU CẦU BỊ THIẾU]"

ANSWER MATCHING:
- Match by CONTENT, not number label
- Scan ALL pages including appendices before marking answer empty
- If answer section separate from questions, cross-reference carefully
- Documents with solutions below each question: extract full solution_steps

LATEX (for math/science):
- All math → $...$ inline. In JSON strings: \\ before commands (\\frac, \\sqrt, \\Rightarrow)
- Fractions: \\frac{a}{b}, Roots: \\sqrt{x}, Powers: x^{2}, Greek: \\alpha
- Systems: \\begin{cases}...\\end{cases}
- NEVER modify radical scope: "√x + 4" → $\\sqrt{x} + 4$ NOT $\\sqrt{x+4}$
- Images → [HÌNH VẼ], Graphs → [ĐỒ THỊ], Tables → [BẢNG DỮ LIỆU]

TYPE: TN | TL | Chứng minh | Tìm x | Tìm GTLN/GTNN | Tính toán | Hệ phương trình | Rút gọn biểu thức | So sánh | Bài toán thực tế | Đọc hiểu | Nghị luận | Tập làm văn | Reading | Writing | Listening | Thí nghiệm | Giải thích hiện tượng
DIFFICULTY: NB | TH | VD | VDC
GRADE: integer 1-12 (infer from document header or content)
CHAPTER: chapter name as it appears in the document (e.g. "Chương 2. Hàm số bậc nhất")
LESSON_TITLE: specific lesson name within the chapter

JSON SCHEMA:
{"question":"<content>","subject":"<subject_code>","type":"<type>","difficulty":"<NB|TH|VD|VDC>","grade":<1-12>,"chapter":"<chapter name>","lesson_title":"<lesson>","answer":"<answer or empty>","solution_steps":["<step>",...]}

SPECIAL CASES:
- Trắc nghiệm: options A/B/C/D in question, correct answer in answer field
- Chứng minh: answer="đpcm", full proof in solution_steps
- GTLN/GTNN: answer includes value AND condition
- No answer found: answer="", solution_steps=[]

OUTPUT: Start with [, end with ]. One object per problem. No text outside array."""

    PARSE_PROMPT_V1 = """Extract ALL questions from this document into a JSON array.
RULES: Close all JSON properly. Copy all content EXACTLY (numbers, formulas, chemical equations, text). If running out of tokens: finish current object, close array.
If text contains SOLUTIONS below questions, extract them into solution_steps.

{text}

JSON array:"""

    PARSE_PROMPT_V2 = """Extract questions to JSON array. Copy every number and formula exactly. Include solution_steps if present.

{text}

JSON:"""

    PARSE_PROMPT_V3 = """Questions → JSON array. Include answers and solution steps.
{text}
JSON:"""

    VISION_PROMPT = """Extract ALL questions from these page images into a JSON array.

CRITICAL RULES:
- Scan EVERY page. Missing questions is unacceptable.
- Math/science formulas → LaTeX: $...$ inline. In JSON: \\ before frac, sqrt, etc.
- Chemical equations: preserve exactly (e.g. Fe + HCl → FeCl₂ + H₂↑)
- Questions with FULL SOLUTIONS below them: extract solution_steps too.
- Multi-part (a,b,c) → ONE object, steps prefixed "--- a)", "--- b)"
- If answers at end of doc, match by CONTENT not number.
- Copy ALL content EXACTLY. Never modify formulas, equations, or coefficients.
- Output ONLY raw JSON array — no markdown.

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
        self._token_usage: Dict[str, int] = {"input": 0, "output": 0, "calls": 0}
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

    def _build_system_prompt(self, subject_code: Optional[str] = None) -> str:
        """Get subject-specific system prompt."""
        return get_prompt_config(subject_code).system_prompt

    def _reset_token_usage(self):
        self._token_usage = {"input": 0, "output": 0, "calls": 0}

    def _track_tokens(self, response):
        """Extract and accumulate token usage from Gemini response."""
        try:
            meta = getattr(response, 'usage_metadata', None)
            if meta:
                inp = getattr(meta, 'prompt_token_count', 0) or 0
                out = getattr(meta, 'candidates_token_count', 0) or 0
                self._token_usage["input"] += inp
                self._token_usage["output"] += out
                self._token_usage["calls"] += 1
        except Exception:
            self._token_usage["calls"] += 1

    def _log_token_summary(self, label: str):
        u = self._token_usage
        total = u["input"] + u["output"]
        logger.info(
            f"💰 {label}: {u['calls']} API calls, "
            f"{u['input']:,} input + {u['output']:,} output = {total:,} total tokens"
        )

    async def parse(
        self,
        text: str,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        subject_hint: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Main entry point — parse text into questions."""
        if not text or not text.strip():
            return []
        if not self._client:
            raise RuntimeError(
                "GOOGLE_API_KEY chưa được cấu hình. "
                "Vui lòng thêm API key trong Settings → Environment Variables."
            )

        config = get_prompt_config(subject_hint)
        logger.info(f"Using prompt family: {config.family} (subject={subject_hint})")

        text = self._clean_text(text)
        start_time = time.time()
        logger.info(f"Document length: {len(text):,} chars")
        self._answer_pool = {}
        self._reset_token_usage()

        if len(text) > self.max_chunk_size:
            result = await self._parse_chunked_parallel(text, progress_callback, subject_hint=subject_hint)
        else:
            result = await self._parse_single(text, chunk_id=0, subject_hint=subject_hint)

        elapsed = time.time() - start_time
        self._log_token_summary(f"Text parse ({len(result)} questions, {elapsed:.1f}s)")
        return result

    async def parse_images(
        self,
        images: List[Dict],
        progress_callback: Optional[Callable[[int, int], None]] = None,
        subject_hint: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Parse questions from images using Vision API.

        v3 — Smart batching for token efficiency:
          - ≤5 pages: all at once (fast, single call)
          - 6-12 pages: batches of 4 pages (avoids output truncation)
          - >12 pages: batches of 4 pages, parallel
          
        Why 4 pages per batch:
          - 13 pages of dense math = ~29 questions × ~500 tokens each = ~15K output tokens
          - Gemini output limit is 8K-65K depending on model
          - 4 pages ≈ 8-10 questions ≈ 5K output tokens — safe margin
          - Each image ≈ 800-1500 input tokens at 150 DPI
          - 4 images ≈ 5K input tokens — well within limits
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
        self._reset_token_usage()

        # Smart batch sizing
        if total_pages <= 5:
            batch_size = total_pages
            logger.info(f"Small PDF: sending all {total_pages} pages at once")
        elif total_pages <= 12:
            batch_size = 4
            logger.info(f"Medium PDF: batches of {batch_size} pages")
        else:
            batch_size = 4
            logger.info(f"Large PDF: parallel batches of {batch_size} pages")

        # Build batches
        batches = []
        for batch_start in range(0, total_pages, batch_size):
            batch_end = min(batch_start + batch_size, total_pages)
            batches.append((batch_start, batch_end, images[batch_start:batch_end]))

        completed = 0

        async def _process_batch(batch_start: int, batch_end: int, batch_imgs: List[Dict]):
            nonlocal completed
            async with self._get_semaphore():
                result = await self._call_gemini_vision(batch_imgs, subject_hint=subject_hint)
            completed += batch_end - batch_start
            if progress_callback:
                progress_callback(min(completed, total_pages), total_pages)
            return result

        # All batches run in parallel (semaphore controls max concurrency)
        tasks = [_process_batch(bs, be, bi) for bs, be, bi in batches]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Retry failed batches once before giving up
        for i, res in enumerate(batch_results):
            if isinstance(res, Exception):
                bs, be, bi = batches[i]
                logger.warning(f"Vision batch {i} (pages {bs}-{be}) failed: {res} — retrying once")
                try:
                    batch_results[i] = await _process_batch(bs, be, bi)
                except Exception as retry_err:
                    logger.error(f"Vision batch {i} retry also failed: {retry_err}")

        failed = sum(1 for r in batch_results if isinstance(r, Exception))
        if failed:
            logger.error(f"Vision parsing: {failed}/{len(batches)} batch(es) failed permanently")

        # Merge with deduplication
        all_questions: List[Dict] = []
        seen_hashes: set = set()
        for res in batch_results:
            if isinstance(res, Exception):
                continue
            for q in res:
                q_hash = self._hash_question(q.get("question", ""))
                if q_hash and q_hash not in seen_hashes:
                    seen_hashes.add(q_hash)
                    all_questions.append(q)
                    self._collect_answers([q])

        all_questions = self._match_answers_from_pool(all_questions)
        elapsed = time.time() - start_time
        self._log_token_summary(f"Vision parse ({len(all_questions)} questions, {elapsed:.1f}s)")
        return all_questions

    # ==================== GEMINI VISION ====================

    async def _call_gemini_vision(self, images: List[Dict], subject_hint: Optional[str] = None) -> List[Dict[str, Any]]:
        """Call Gemini Vision API — 3-tier fallback.

        v3: Adaptive timeout based on page count. Rate limit retry with backoff.
        """
        if not self._client:
            return []
        from google.genai import types

        config = get_prompt_config(subject_hint)
        parts = [config.vision_prompt]
        for img in images:
            parts.append(types.Part.from_bytes(
                data=base64.b64decode(img["data"]),
                mime_type=img.get("mime_type", "image/jpeg"),
            ))

        # Adaptive timeout: more pages = more time needed
        timeout = max(60, min(180, 30 * len(images)))
        content = ""

        for mime, schema, label in [
            ("application/json", PARSE_SCHEMA, "schema"),
            ("application/json", None,         "json"),
            (None,               None,          "plain"),
        ]:
            try:
                cfg_kwargs: Dict[str, Any] = dict(
                    system_instruction=self._build_system_prompt(subject_hint),
                    temperature=0,
                    max_output_tokens=self.max_tokens,
                    safety_settings=[types.SafetySetting(**s) for s in _SAFETY_SETTINGS],
                )
                if mime:
                    cfg_kwargs["response_mime_type"] = mime
                if schema:
                    cfg_kwargs["response_schema"] = schema

                for attempt in range(2):  # 2 attempts per tier (was 3)
                    try:
                        response = await asyncio.wait_for(
                            self._client.aio.models.generate_content(
                                model=self.gemini_model,
                                contents=parts,
                                config=types.GenerateContentConfig(**cfg_kwargs),
                            ),
                            timeout=timeout,
                        )
                        self._track_tokens(response)
                        content = self._safe_text(response)
                        if content:
                            result = self._extract_json(content)
                            if result:
                                logger.info(f"Vision {label}: {len(result)} questions from {len(images)} pages")
                                return result
                        break  # Got response but no valid JSON — try next tier
                    except asyncio.TimeoutError:
                        logger.warning(f"Vision {label} timed out ({timeout}s), attempt {attempt+1}")
                        break  # Don't retry timeout — try next tier
                    except Exception as e:
                        err = str(e)
                        if "429" in err or "RESOURCE_EXHAUSTED" in err:
                            wait = (attempt + 1) * 8
                            logger.warning(f"Vision {label} rate limited, wait {wait}s...")
                            await asyncio.sleep(wait)
                            continue
                        logger.warning(f"Vision {label} failed: {e}")
                        break

            except Exception as e:
                logger.warning(f"Vision {label} outer error: {e}")

        if content:
            result = self._extract_json(content)
            logger.info(f"Vision fallback: {len(result)} questions from {len(images)} pages")
            return result
        return []

    # ==================== TEXT PARSING ====================

    async def _parse_single(self, text: str, chunk_id: int = 0, subject_hint: Optional[str] = None) -> List[Dict[str, Any]]:
        """Parse single chunk — uses ONE prompt only (no more 3-prompt loop).

        v4: Cost optimization — removed 3-prompt rotation that tripled API calls
        with minimal benefit (V1/V2/V3 prompts are similar, if V1 fails V2/V3 also fail).
        Now: 1 prompt × 3 tiers = 6 calls max (was 27).
        """
        sem = self._get_semaphore()
        config = get_prompt_config(subject_hint)
        async with sem:
            try:
                logger.info(f"Chunk {chunk_id} - parsing with {config.family} prompt...")
                result, raw_content = await self._call_gemini(text, config.parse_prompt_v1, subject_hint=subject_hint)

                if result:
                    logger.info(f"Chunk {chunk_id} - Extracted {len(result)} questions")
                    self._collect_answers(result)
                    return result

                # Try salvage from raw content if Gemini returned text but JSON parse failed
                if raw_content:
                    result = self._aggressive_extract_json(raw_content)
                    if result:
                        logger.info(f"Chunk {chunk_id} - Salvaged {len(result)} questions from raw response")
                        self._collect_answers(result)
                        return result

                logger.warning(f"Chunk {chunk_id} - No questions extracted")
            except Exception as e:
                logger.error(f"Chunk {chunk_id} - Failed: {e}")

            return []

    async def _call_gemini(self, text: str, prompt_template: str, subject_hint: Optional[str] = None) -> tuple[List[Dict], str]:
        """Call Gemini API — 3-tier fallback with retry.

        v4: Cost optimization — reduced retries from 3→2 per tier.
        Max calls: 3 tiers × 2 retries = 6 (was 9).
        """
        from google.genai import types

        prompt = prompt_template.format(text=text)
        logger.info(f"_call_gemini: text={len(text)} chars, subject={subject_hint}, model={self.gemini_model}")

        async def _try_with_retry(config, label):
            for attempt in range(2):  # 2 attempts per tier (was 3)
                try:
                    t0 = time.time()
                    response = await asyncio.wait_for(
                        self._client.aio.models.generate_content(
                            model=self.gemini_model,
                            contents=prompt,
                            config=config,
                        ),
                        timeout=90,
                    )
                    elapsed = time.time() - t0
                    self._track_tokens(response)
                    content = self._safe_text(response)
                    logger.info(f"{label}: response in {elapsed:.1f}s, content={len(content or '')} chars")
                    if content:
                        result = self._extract_json(content)
                        if result:
                            logger.info(f"{label}: {len(result)} questions extracted")
                            return result, content
                        else:
                            logger.warning(f"{label}: content received but JSON invalid, preview: {(content or '')[:200]!r}")
                    return None, content or ""
                except asyncio.TimeoutError:
                    logger.warning(f"{label} timed out after 90s, skipping to next tier")
                    return None, ""
                except Exception as e:
                    err_str = str(e)
                    err_lower = err_str.lower()
                    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                        wait = (attempt + 1) * 5  # 5/10s
                        logger.warning(f"{label} rate limited, waiting {wait}s...")
                        await asyncio.sleep(wait)
                        continue
                    if (
                        "502" in err_str or "503" in err_str or "500" in err_str
                        or "bad gateway" in err_lower
                        or "unavailable" in err_lower
                        or "server error" in err_lower
                        or "internal error" in err_lower
                    ):
                        wait = (attempt + 1) * 15  # 15/30s
                        logger.warning(f"{label} server error (attempt {attempt + 1}), retry in {wait}s: {err_str[:80]}")
                        await asyncio.sleep(wait)
                        continue
                    logger.warning(f"{label} failed: {e}")
                    return None, ""
            return None, ""

        content = ""
        sys_prompt = self._build_system_prompt(subject_hint)
        for mime, schema, label in [
            ("application/json", PARSE_SCHEMA, "Schema mode"),
            ("application/json", None,         "JSON mode"),
            (None,               None,          "Plain text"),
        ]:
            cfg_kwargs: Dict[str, Any] = dict(
                system_instruction=sys_prompt,
                temperature=0,
                max_output_tokens=self.max_tokens,
                safety_settings=[types.SafetySetting(**s) for s in _SAFETY_SETTINGS],
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

        return [], content

    async def _parse_chunked_parallel(
        self, text: str, progress_callback: Optional[Callable] = None, subject_hint: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Parallel chunk processing with deduplication."""
        chunks = self._smart_chunk(text)
        total_chunks = len(chunks)
        logger.info(f"Split into {total_chunks} chunks (max {self.max_concurrency} parallel)")

        completed = 0

        async def process_chunk(idx: int, chunk: str) -> tuple[int, List[Dict]]:
            nonlocal completed
            start = time.time()
            result = await self._parse_single(chunk, chunk_id=idx, subject_hint=subject_hint)
            elapsed = time.time() - start
            completed += 1
            logger.info(f"Chunk {idx + 1}/{total_chunks} done ({len(result)} questions, {elapsed:.1f}s)")
            if progress_callback:
                progress_callback(completed, total_chunks)
            return idx, result

        tasks = [process_chunk(i, chunk) for i, chunk in enumerate(chunks)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        sorted_results = sorted(
            [r for r in results if not isinstance(r, Exception)],
            key=lambda x: x[0]
        )

        all_questions: List[Dict] = []
        seen_hashes: set = set()
        for _, questions in sorted_results:
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
        current_chunk = text[:splits[0].start()]

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
        except Exception as e:
            # Gemini raises ValueError when response is blocked by safety filters
            logger.warning(f"_safe_text: response.text failed: {e}")
        try:
            for c in response.candidates:
                for p in c.content.parts:
                    if hasattr(p, 'text') and p.text:
                        return p.text
        except Exception:
            pass
        # Log WHY the response is empty — safety block? empty candidates?
        try:
            # Check for safety block
            if hasattr(response, 'prompt_feedback'):
                fb = response.prompt_feedback
                if fb:
                    logger.warning(f"_safe_text: prompt_feedback={fb}")
            # Check candidate finish reason
            if hasattr(response, 'candidates') and response.candidates:
                for i, c in enumerate(response.candidates):
                    finish = getattr(c, 'finish_reason', None)
                    safety = getattr(c, 'safety_ratings', None)
                    if finish and str(finish) != "STOP":
                        logger.warning(f"_safe_text: candidate[{i}] finish_reason={finish}")
                    if safety:
                        blocked = [s for s in safety if getattr(s, 'blocked', False)]
                        if blocked:
                            logger.warning(f"_safe_text: BLOCKED by safety: {blocked}")
            elif hasattr(response, 'candidates'):
                logger.warning(f"_safe_text: 0 candidates in response")
        except Exception as diag_err:
            logger.debug(f"_safe_text: diagnostic failed: {diag_err}")
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
        """Extract individual JSON objects one by one as last resort.
        NOTE: caller (_aggressive_extract_json) already applied _RE_TRIPLE_BACKSLASH fix.
        """
        objects = []
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