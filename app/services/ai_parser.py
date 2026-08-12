"""
AI Question Parser — Phân tích đề toán bằng Gemini API (text-only).

Gemini Vision đã được loại bỏ. Mọi luồng OCR đi qua Marker/Docling/Pix2Text
ở pipeline.py. Module này chỉ nhận text/markdown đã OCR và yêu cầu Gemini
trả về JSON câu hỏi.
"""

import os
import json
import asyncio
import re
import time
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


# ── Structured output schema (math/science) ──
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

# ── IELTS structured output schema ──
IELTS_PARSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "section_title":     {"type": "STRING"},
            "passage_text":      {"type": "STRING"},
            "group_instruction": {"type": "STRING"},
            "word_limit":        {"type": "STRING"},
            "global_number":     {"type": "INTEGER"},
            "question_text":     {"type": "STRING"},
            "question_type":     {"type": "STRING"},
            "answer":            {"type": "STRING"},
            "choices_json":      {"type": "STRING"},
            "items_json":        {"type": "STRING"},
            "points":            {"type": "NUMBER"},
        },
        "required": [
            "question_text", "question_type", "answer", "global_number",
        ],
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

# ── Answer-key extraction (trích {cau_num: answer} từ phần ĐÁP ÁN) ──
_ANSWER_KEY_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "cau_num": {"type": "INTEGER"},
            "answer": {"type": "STRING"},
        },
        "required": ["cau_num", "answer"],
    },
}

_ANSWER_KEY_PROMPT = r"""Đây là phần ĐÁP ÁN / HƯỚNG DẪN CHẤM của một đề thi K12 Việt Nam.
Với MỖI câu (Câu N / Bài N), trích ĐÁP ÁN CUỐI CÙNG, NGẮN GỌN:
- Trắc nghiệm: 1 chữ cái "A"/"B"/"C"/"D".
- Tính toán/tìm x: giá trị cuối, ví dụ "x = 2", "$\frac{23}{45}$", "5 cm", "12".
- Bài chứng minh không có đáp số → answer = "đpcm".
GIỮ NGUYÊN LaTeX trong $...$. KHÔNG chép cả lời giải, chỉ lấy kết quả cuối.
Trả JSON array: [{"cau_num": <số>, "answer": "<đáp án>"}]. KHÔNG giải thích, KHÔNG markdown.

ĐÁP ÁN:
{text}"""

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

# B4 — Token estimator cho adaptive max_output_tokens
_RE_QUESTION_MARKER = re.compile(
    r'(?:^|\n)\s*(?:Câu|Bài|Question)\s+\d+|(?:^|\n)\s*\d+[.)]\s+',
    re.IGNORECASE,
)
# Capturing variant để lấy SỐ câu (dedup distinct) cho completeness estimate.
_RE_QUESTION_MARKER_NUM = re.compile(
    r'(?:^|\n)\s*(?:Câu|Bài|Question)\s+(\d+)|(?:^|\n)\s*(\d+)[.)]\s+',
    re.IGNORECASE,
)


def _estimate_question_count(text: str) -> int:
    """Ước tính số câu hỏi DISTINCT qua regex marker.

    Phase 3.7: đề thi VN thường lặp 'Câu N' ở cả phần đề và 'Hướng dẫn chấm'
    → đếm raw marker sẽ gấp đôi → completeness_ratio tụt → cảnh báo 'thiếu câu'
    GIẢ. Đếm distinct số câu để khử lặp. Fallback raw count nếu không parse được số.
    """
    if not text:
        return 0
    nums: set[int] = set()
    raw = 0
    for m in _RE_QUESTION_MARKER_NUM.finditer(text):
        raw += 1
        g = m.group(1) or m.group(2)
        if g:
            try:
                nums.add(int(g))
            except (TypeError, ValueError):
                pass
    return len(nums) or raw


def _adaptive_output_tokens(text: str, ceiling: int = 32768, floor: int = 4096) -> int:
    """Tính max_output_tokens dựa trên số marker (raw) trong text.

    Mỗi câu trung bình ~800 output tokens (đề + answer + 5-8 solution steps
    khi parse cả "Hướng dẫn chấm"). Cộng 4096 buffer cho overhead JSON +
    long LaTeX strings. Dùng RAW marker count (gồm cả lặp ở đáp án) làm ước
    lượng phần trên để over-allocate an toàn, tránh truncation; ceiling 32K
    (Gemini Flash hỗ trợ 65K output), floor 4K.
    """
    raw_markers = len(_RE_QUESTION_MARKER.findall(text))
    return max(floor, min(ceiling, raw_markers * 800 + 4096))


# D3 — pattern để extract question number từ parsed question text
_RE_PARSED_Q_NUM = re.compile(
    r'^\s*(?:Câu|Bài|Question)\s*(\d+)|^\s*(\d+)\s*[.)]',
    re.IGNORECASE,
)


def _extract_question_number(q_text: str) -> Optional[int]:
    """Extract câu number từ text. Return None nếu không phát hiện."""
    if not q_text:
        return None
    m = _RE_PARSED_Q_NUM.match(q_text)
    if not m:
        return None
    num_str = m.group(1) or m.group(2)
    try:
        return int(num_str)
    except (TypeError, ValueError):
        return None


def _repair_latex_escapes(s: str) -> str:
    r"""Khôi phục LaTeX bị JSON ăn mất backslash.

    Khi Gemini emit `\frac` (1 backslash) trong JSON string, `json.loads` hiểu
    `\f` là form-feed → `$\x0crac{1}{2}$`. Các control char này KHÔNG bao giờ hợp
    lệ trong text đề/đáp án nên khôi phục về dạng `\f`, `\b`, `\v`, `\r`, `\t`
    (tức `\frac`, `\beta`, `\times`...). Không đụng `\n` (xuống dòng có thể hợp lệ).
    """
    if not s or not isinstance(s, str):
        return s
    # \x0c/\x08/\x0b không bao giờ hợp lệ → luôn khôi phục.
    s = s.replace("\x0c", "\\f").replace("\x08", "\\b").replace("\x0b", "\\v")
    # \r (CR) và \t (TAB) chỉ khôi phục khi có ngữ cảnh toán ($...$) để tránh
    # đụng tab/CR hợp lệ trong văn bản thường.
    if "$" in s:
        s = s.replace("\r", "\\r").replace("\t", "\\t")
    return s


def _merge_duplicate_cau_num(questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Gộp các câu TRÙNG cau_num thành 1 object.

    Đề thi VN thường có phần ĐỀ (Câu 1..N) + phần ĐÁP ÁN/HƯỚNG DẪN CHẤM lặp lại
    cùng số Câu. Gemini đôi khi emit cả 2 → đề và đáp án bị tách thành 2 câu khác
    nhau (hoặc khi chunk, đề & đáp án nằm 2 chunk → 2 entry). Hàm này deterministic
    gộp theo cau_num: giữ "question" từ lần xuất hiện ĐẦU (đề), lấy answer/
    solution_steps từ entry có nội dung (đáp án). Câu không parse được cau_num giữ nguyên.
    """
    if not questions or len(questions) < 2:
        return questions
    merged: Dict[int, Dict[str, Any]] = {}
    order: List[int] = []
    passthrough: List[Dict[str, Any]] = []
    collapsed = 0
    for q in questions:
        n = _extract_question_number(str(q.get("question") or ""))
        if n is None:
            passthrough.append(q)
            continue
        if n not in merged:
            merged[n] = dict(q)
            order.append(n)
            continue
        # Trùng cau_num → gộp vào entry đầu (đề).
        collapsed += 1
        base = merged[n]
        if not str(base.get("answer") or "").strip() and str(q.get("answer") or "").strip():
            base["answer"] = q.get("answer")
            if q.get("answer_source"):
                base["answer_source"] = q.get("answer_source")
        new_steps = q.get("solution_steps") or []
        if len(new_steps) > len(base.get("solution_steps") or []):
            base["solution_steps"] = new_steps
        # Nếu entry đầu thiếu question text mà entry sau đầy đủ hơn → dùng cái dài hơn.
        if len(str(q.get("question") or "")) > len(str(base.get("question") or "")) * 2:
            base["question"] = q.get("question")
    if collapsed:
        logger.info("Merged %d duplicate cau_num entries (đề + đáp án) → %d unique", collapsed, len(order))
    return [merged[n] for n in order] + passthrough


def _detect_numbering_gaps(questions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """D3: phát hiện numbering gap (Q1, Q2, Q4 → mất Q3).

    Returns:
        {
            "numbered_questions": int,           # số câu extract được số thứ tự
            "min_number": int | None,
            "max_number": int | None,
            "missing_numbers": list[int],        # các số thiếu trong dải [min, max]
            "duplicate_numbers": list[int],      # số xuất hiện > 1 lần
        }
    """
    seen: Dict[int, int] = {}
    for q in questions:
        n = _extract_question_number(q.get("question", ""))
        if n is not None:
            seen[n] = seen.get(n, 0) + 1
    if not seen:
        return {
            "numbered_questions": 0,
            "min_number": None,
            "max_number": None,
            "missing_numbers": [],
            "duplicate_numbers": [],
        }
    nums = sorted(seen.keys())
    lo, hi = nums[0], nums[-1]
    full_range = set(range(lo, hi + 1))
    missing = sorted(full_range - set(nums))
    duplicates = sorted([n for n, c in seen.items() if c > 1])
    return {
        "numbered_questions": sum(seen.values()),
        "min_number": lo,
        "max_number": hi,
        "missing_numbers": missing,
        "duplicate_numbers": duplicates,
    }


# ── A2: targeted re-extraction of individually-missing question numbers ──
# After D3 finds gaps (e.g. Q3 missing because it split across chunks), re-parse
# just the text region around each missing "Câu N" instead of re-queuing whole
# chunks. Bounded by AI_REEXTRACT_MAX to cap extra Gemini calls / cost.
_REEXTRACT_MISSING_ENABLED = os.getenv("AI_REEXTRACT_MISSING", "1").strip().lower() not in {"0", "false", "no", "off"}
_REEXTRACT_MAX = int(os.getenv("AI_REEXTRACT_MAX", "12").split("#", 1)[0].strip() or "12")


def _slice_question_region(text: str, n: int) -> str:
    """Return text for question number ``n`` — from its marker up to the next
    question marker (any number) or end of text. Empty string if not found."""
    if not text:
        return ""
    starts: list[tuple[int, int]] = []
    for m in _RE_QUESTION_MARKER_NUM.finditer(text):
        g = m.group(1) or m.group(2)
        try:
            starts.append((int(g), m.start()))
        except (TypeError, ValueError):
            continue
    for i, (num, st) in enumerate(starts):
        if num == n:
            end = starts[i + 1][1] if i + 1 < len(starts) else len(text)
            return text[st:end].strip()
    return ""


async def recover_missing_questions(
    parse_single: Callable[[str], Any],
    text: str,
    missing_numbers: List[int],
    subject_hint: Optional[str] = None,
    cap: int = _REEXTRACT_MAX,
) -> List[Dict[str, Any]]:
    """Re-extract each missing question number individually.

    ``parse_single`` is an async callable ``(region_text) -> list[dict]`` (in
    production: ``AIQuestionParser._parse_single``). Returns recovered question
    dicts whose extracted number matches the target (or the sole item of a
    single-question region). Best-effort: per-number failures are swallowed.
    """
    recovered: List[Dict[str, Any]] = []
    for n in (missing_numbers or [])[:cap]:
        region = _slice_question_region(text, n)
        if len(region.strip()) < 20:
            continue
        try:
            items = await parse_single(region)
        except Exception as e:
            logger.debug("A2 recover Q%s failed: %s", n, e)
            continue
        if not items:
            continue
        for it in items:
            if not isinstance(it, dict) or not str(it.get("question") or "").strip():
                continue
            num = _extract_question_number(str(it.get("question") or ""))
            if num == n or len(items) == 1:
                recovered.append(it)
    return recovered


class GeminiFatalError(RuntimeError):
    """Non-retryable Gemini error (bad key / quota / invalid request, HTTP
    400/401/403). Aborts the whole parse session instead of being swallowed
    per-chunk — otherwise every remaining chunk keeps hitting a dead API."""


class AIQuestionParser:
    """
    Parser sử dụng Gemini API để phân tích đề toán.
    """

    # Class-level fallback prompts đã thay thế bởi subject_prompts.PROMPT_CONFIGS.
    # `_build_system_prompt` luôn lấy family-specific prompt, nên không có biến
    # class-level system/parse cho cần lưu trữ.

    def __init__(
        self,
        provider: AIProvider = AIProvider.GEMINI,
        gemini_api_key: Optional[str] = None,
        gemini_model: str = None,
        max_tokens: int = 65536,
        max_chunk_size: int = 20000,
        max_concurrency: int = 3,
    ):
        from app.core.config import settings
        self.provider = provider
        self.gemini_api_key = gemini_api_key or os.getenv("GOOGLE_API_KEY")
        self.gemini_model = gemini_model or settings.GEMINI_MODEL
        self.max_tokens = max_tokens
        self.max_chunk_size = max_chunk_size
        self.max_concurrency = max_concurrency

        # OPT: Lazy-init semaphore — avoids "attached to different event loop" error
        # when singleton is created at module import time (before uvicorn starts loop)
        self._semaphore: Optional[asyncio.Semaphore] = None

        self._answer_pool: Dict[str, str] = {}
        self._token_usage: Dict[str, int] = {"input": 0, "output": 0, "calls": 0}
        self._fatal_error: Optional[Exception] = None  # C2: set on non-retryable Gemini error
        # D1-D3: quality report cập nhật sau mỗi parse, caller có thể đọc qua
        # `ai_parser._last_quality_report` để emit SSE.
        self._last_quality_report: Dict[str, Any] = {}
        self._client = None
        self._init_clients()

        # B1 — Gemini context cache: re-use system prompt giữa các call trong cùng
        # parse session. Mỗi subject family có 1 cache riêng (key by prompt hash).
        # Cache TTL ngắn (5 phút mặc định) để không giữ state lâu trên server Google.
        self._cache_enabled = os.getenv("GEMINI_CONTEXT_CACHE", "1") not in ("0", "false", "False")
        self._cache_ttl_seconds = int(os.getenv("GEMINI_CONTEXT_CACHE_TTL", "300"))
        # Map: prompt_hash → (cache_name, created_at). None = previously tried & failed.
        self._prompt_cache: Dict[str, Optional[tuple]] = {}
        self._cache_lock: Optional[asyncio.Lock] = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        """Lazy-create semaphore in the current running event loop."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
        return self._semaphore

    def _get_cache_lock(self) -> asyncio.Lock:
        """Lazy-init lock — tránh race khi nhiều chunk cùng tạo cache."""
        if self._cache_lock is None:
            self._cache_lock = asyncio.Lock()
        return self._cache_lock

    @staticmethod
    def _hash_prompt(text: str) -> str:
        """Hash short stable cho prompt → cache key."""
        import hashlib

        return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]

    async def _get_or_create_cache(self, system_prompt: str) -> Optional[str]:
        """B1: trả cache name (server-side) cho system_prompt cho trước.

        Trả `None` nếu caching không khả dụng (SDK lỗi, prompt quá nhỏ,
        feature flag tắt). Caller cần fallback về `system_instruction`
        truyền thống khi nhận `None`.
        """
        if not self._cache_enabled or not self._client:
            return None
        key = self._hash_prompt(system_prompt)

        # Fast path: đã có cache hợp lệ trong session này
        entry = self._prompt_cache.get(key)
        if entry is not None:
            cache_name, created_at = entry
            if cache_name is None:
                return None  # đã thử và fail → không retry
            if time.time() - created_at < self._cache_ttl_seconds - 30:
                return cache_name
            # gần hết hạn → drop và tạo lại
            self._prompt_cache.pop(key, None)

        async with self._get_cache_lock():
            # Re-check sau khi lock — chunk khác có thể đã tạo
            entry = self._prompt_cache.get(key)
            if entry is not None and entry[0] is not None:
                cache_name, created_at = entry
                if time.time() - created_at < self._cache_ttl_seconds - 30:
                    return cache_name
            try:
                from google.genai import types as _types

                cfg = _types.CreateCachedContentConfig(
                    system_instruction=system_prompt,
                    ttl=f"{self._cache_ttl_seconds}s",
                )
                cache = await self._client.aio.caches.create(
                    model=self.gemini_model,
                    config=cfg,
                )
                cache_name = getattr(cache, "name", None)
                if not cache_name:
                    logger.warning("Cache created but name missing — fallback no-cache")
                    self._prompt_cache[key] = (None, time.time())
                    return None
                self._prompt_cache[key] = (cache_name, time.time())
                logger.info(
                    "Gemini context cache created: name=%s, ttl=%ds, prompt=%d chars",
                    cache_name, self._cache_ttl_seconds, len(system_prompt),
                )
                return cache_name
            except Exception as exc:
                # Nguyên nhân phổ biến: prompt < min cache size (1024 tokens cho Flash),
                # quota, network. Mark fail để không retry trong session.
                logger.info(
                    "Gemini context cache không khả dụng (%s) — fallback system_instruction",
                    str(exc)[:120],
                )
                self._prompt_cache[key] = (None, time.time())
                return None

    async def cleanup_caches(self) -> None:
        """Best-effort xóa cache đã tạo trong session. Gọi cuối parse session."""
        if not self._client or not self._prompt_cache:
            return
        for key, entry in list(self._prompt_cache.items()):
            if entry is None or entry[0] is None:
                continue
            cache_name = entry[0]
            try:
                await self._client.aio.caches.delete(name=cache_name)
                logger.debug("Deleted Gemini cache %s", cache_name)
            except Exception as exc:
                logger.debug("Cache delete failed (TTL sẽ tự dọn): %s", exc)
        self._prompt_cache.clear()

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
        self._fatal_error = None  # C2: cleared per parse session

        # D1: ước tính số câu hỏi trước khi parse
        estimated = _estimate_question_count(text)
        logger.info(f"D1: estimated {estimated} questions in document")

        # B6: dùng token-aware target làm threshold cắt chunk (nhỏ hơn max_chunk_size)
        chunk_threshold = self._target_chunk_chars()
        if len(text) > chunk_threshold:
            result = await self._parse_chunked_parallel(text, progress_callback, subject_hint=subject_hint)
        else:
            result = await self._parse_single(text, chunk_id=0, subject_hint=subject_hint)

        # Phase 3.8: backfill `subject` từ ngữ cảnh parse cho item bị thiếu (đặc
        # biệt nhánh salvage `_extract_individual_objects` không set subject) →
        # tránh downstream mặc định "toan" làm sai môn Hoá/Sinh.
        _default_subject = subject_hint or "toan"
        for _item in result:
            if isinstance(_item, dict) and "question" in _item and not _item.get("subject"):
                _item["subject"] = _default_subject

        # Khôi phục LaTeX bị JSON ăn backslash (\frac→form-feed) trên question/
        # answer/solution_steps.
        for _it in result:
            if not isinstance(_it, dict):
                continue
            if _it.get("question"):
                _it["question"] = _repair_latex_escapes(_it["question"])
            if _it.get("answer"):
                _it["answer"] = _repair_latex_escapes(_it["answer"])
            _steps = _it.get("solution_steps")
            if isinstance(_steps, list):
                _it["solution_steps"] = [_repair_latex_escapes(str(s)) for s in _steps]

        # Gộp câu trùng cau_num (đề + ĐÁP ÁN/HƯỚNG DẪN CHẤM lặp lại số câu, hoặc
        # đề & đáp án rơi 2 chunk) → tránh đề/đáp án bị tách thành 2 câu khác nhau.
        result = _merge_duplicate_cau_num(result)

        # D2+D3: post-parse validation + numbering gap detection
        parsed_count = len(result)
        gap_info = _detect_numbering_gaps(result)

        # A2: re-extract individually-missing question numbers (Q split across
        # chunks / dropped). Bounded; skips when too many missing (systemic OCR
        # failure, not worth N extra calls).
        recovered_count = 0
        if (
            gap_info["missing_numbers"]
            and _REEXTRACT_MISSING_ENABLED
            and len(gap_info["missing_numbers"]) <= _REEXTRACT_MAX
        ):
            try:
                recovered = await recover_missing_questions(
                    lambda region: self._parse_single(region, chunk_id=-1, subject_hint=subject_hint),
                    text, gap_info["missing_numbers"], subject_hint=subject_hint,
                )
                if recovered:
                    for _it in recovered:
                        if not isinstance(_it, dict):
                            continue
                        if not _it.get("subject"):
                            _it["subject"] = _default_subject
                        if _it.get("question"):
                            _it["question"] = _repair_latex_escapes(_it["question"])
                        if _it.get("answer"):
                            _it["answer"] = _repair_latex_escapes(_it["answer"])
                    result = _merge_duplicate_cau_num(result + recovered)
                    recovered_count = len(recovered)
                    parsed_count = len(result)
                    gap_info = _detect_numbering_gaps(result)
                    logger.info("A2: recovered %d missing question(s)", recovered_count)
            except Exception as _a2e:
                logger.debug("A2 re-extract skipped: %s", _a2e)

        completeness_ratio = (parsed_count / estimated) if estimated > 0 else 1.0
        # D2: warn nếu parsed < 80% estimated
        warning_threshold = 0.80
        is_incomplete = estimated > 0 and completeness_ratio < warning_threshold

        self._last_quality_report = {
            "estimated_count": estimated,
            "parsed_count": parsed_count,
            "completeness_ratio": round(completeness_ratio, 3),
            "is_incomplete": is_incomplete,
            "missing_numbers": gap_info["missing_numbers"],
            "duplicate_numbers": gap_info["duplicate_numbers"],
            "number_range": [gap_info["min_number"], gap_info["max_number"]],
            "numbered_questions": gap_info["numbered_questions"],
            "recovered_count": recovered_count,
        }

        if is_incomplete:
            logger.warning(
                "D2 incomplete parse: %d/%d (%.0f%%) — missing numbers: %s",
                parsed_count, estimated, completeness_ratio * 100,
                gap_info["missing_numbers"][:20],
            )
        if gap_info["missing_numbers"]:
            logger.warning(
                "D3 numbering gap detected: missing Q%s (range %s-%s)",
                gap_info["missing_numbers"][:20],
                gap_info["min_number"], gap_info["max_number"],
            )

        elapsed = time.time() - start_time
        self._log_token_summary(f"Text parse ({len(result)} questions, {elapsed:.1f}s)")
        return result

    # ==================== ANSWER-KEY EXTRACTION ====================

    async def extract_answer_key(
        self, answer_key_text: str, subject_hint: Optional[str] = None
    ) -> Dict[int, str]:
        """Trích {cau_num: answer} từ phần ĐÁP ÁN/HƯỚNG DẪN CHẤM bằng Gemini.

        Output rất nhỏ (chỉ số câu + đáp án ngắn) → JSON ổn định, dùng cho cả câu
        tự luận (Gemini đọc lời giải rút đáp án cuối — việc regex làm kém). Trả
        dict rỗng nếu không có đáp án / lỗi (không raise).
        """
        if not answer_key_text or not answer_key_text.strip() or not self._client:
            return {}
        from google.genai import types

        # .replace (KHÔNG .format) vì prompt chứa LaTeX có dấu { } (vd $\frac{1}{2}$).
        prompt = _ANSWER_KEY_PROMPT.replace("{text}", answer_key_text[:30000])
        cfg = types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=4096,
            response_mime_type="application/json",
            response_schema=_ANSWER_KEY_SCHEMA,
            safety_settings=[types.SafetySetting(**s) for s in _SAFETY_SETTINGS],
        )
        try:
            resp = await asyncio.wait_for(
                self._client.aio.models.generate_content(
                    model=self.gemini_model, contents=prompt, config=cfg
                ),
                timeout=90,
            )
            self._track_tokens(resp)
            data = self._extract_json(self._safe_text(resp))
            out: Dict[int, str] = {}
            for it in data or []:
                if not isinstance(it, dict):
                    continue
                n = it.get("cau_num")
                a = _repair_latex_escapes(str(it.get("answer") or "")).strip()
                if isinstance(n, int) and a:
                    out[n] = a
            logger.info("extract_answer_key: %d đáp án trích từ answer-key section", len(out))
            return out
        except Exception as e:
            logger.warning(f"extract_answer_key failed: {e}")
            return {}

    # ==================== IELTS PARSE ====================

    async def parse_ielts(
        self,
        text: str,
        progress_callback: Optional[Callable] = None,
    ) -> List[Dict[str, Any]]:
        """Parse a full IELTS exam in a single Gemini call.

        Returns a flat list where each item is one question with
        section_title, passage_text, group_instruction, and other IELTS fields.
        No chunking: Gemini 2.5 Flash 1M context handles a full IELTS exam easily.
        """
        if not text or not text.strip():
            return []
        if not self._client:
            raise RuntimeError("GOOGLE_API_KEY chưa được cấu hình.")

        config = get_prompt_config("ielts")
        text = self._clean_text(text)
        logger.info(f"IELTS parse: {len(text):,} chars")
        self._reset_token_usage()

        if progress_callback:
            await progress_callback(10, "Đang gửi đề thi đến Gemini...")

        chunks = self._split_ielts_text_chunks(text)
        results: List[Dict[str, Any]] = []
        seen: set[tuple[Any, str]] = set()
        for idx, chunk in enumerate(chunks):
            if progress_callback:
                pct = 10 + int((idx / max(len(chunks), 1)) * 65)
                await progress_callback(pct, f"Gemini text dang chuan hoa phan {idx + 1}/{len(chunks)}...")

            chunk_result, _ = await self._call_gemini(
                text=chunk,
                prompt_template=config.parse_prompt_v1,
                subject_hint="ielts",
                override_schema=IELTS_PARSE_SCHEMA,
            )
            for item in chunk_result or []:
                key = (
                    item.get("global_number"),
                    self._hash_question(item.get("question_text", "")),
                )
                if key in seen:
                    continue
                seen.add(key)
                results.append(item)

        result = sorted(
            results,
            key=lambda q: int(q.get("global_number") or 9999),
        )

        if progress_callback:
            await progress_callback(80, f"Gemini trả về {len(result or [])} câu hỏi")

        self._log_token_summary(f"IELTS parse ({len(result or [])} questions)")
        return result or []

    def _split_ielts_text_chunks(self, text: str, max_chars: int = 12000) -> List[str]:
        """Split IELTS OCR text into section/group-sized chunks for lower token cost."""
        clean = text.strip()
        if len(clean) <= max_chars:
            return [clean]

        pattern = re.compile(
            r"(?im)(?=^\s*(?:reading passage\s+\d+|listening\s+section\s+\d+|section\s+\d+|"
            r"writing task\s+\d+|part\s+\d+|questions\s+\d+\s*[-–]\s*\d+))"
        )
        parts = [part.strip() for part in pattern.split(clean) if part.strip()]
        if len(parts) <= 1:
            return [clean[i:i + max_chars] for i in range(0, len(clean), max_chars)]

        chunks: List[str] = []
        current = ""
        for part in parts:
            if current and len(current) + len(part) + 2 > max_chars:
                chunks.append(current.strip())
                current = part
            else:
                current = f"{current}\n\n{part}".strip() if current else part
        if current:
            chunks.append(current.strip())
        return chunks

    # ==================== TEXT PARSING ====================

    async def _parse_single_emphatic(
        self, text: str, chunk_id: int = 0, subject_hint: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """D4 retry: parse chunk với prompt nhấn mạnh khi lần đầu trả empty.

        Khác `_parse_single` ở 3 điểm:
          1. Wrap text với chỉ dẫn "đoạn này CÓ câu hỏi, hãy extract bằng được"
          2. Bypass schema mode → đi thẳng plain text (tiết kiệm 1 call)
          3. Log warning để theo dõi tần suất retry
        """
        sem = self._get_semaphore()
        config = get_prompt_config(subject_hint)
        emphatic_prompt = (
            "Đoạn văn bản sau CHẮC CHẮN có ít nhất một câu hỏi/bài tập "
            "(đã có người xem trước). Hãy extract THẬT KỸ tất cả các câu hỏi, "
            "kể cả khi định dạng bất thường (không có 'Câu N.', đáp án rời, ...). "
            "Nếu không tìm thấy số thứ tự, hãy đánh số tự động.\n\n"
            + config.parse_prompt_v1
        )
        async with sem:
            try:
                logger.info(f"D4 emphatic retry chunk {chunk_id}")
                result, raw = await self._call_gemini(
                    text, emphatic_prompt, subject_hint=subject_hint,
                )
                if result:
                    self._collect_answers(result)
                    logger.info(f"D4 retry chunk {chunk_id}: recovered {len(result)} questions")
                    return result
                if raw:
                    salvaged = self._aggressive_extract_json(raw)
                    if salvaged:
                        self._collect_answers(salvaged)
                        logger.info(f"D4 retry chunk {chunk_id}: salvaged {len(salvaged)} questions")
                        return salvaged
            except Exception as e:
                logger.warning(f"D4 retry chunk {chunk_id} failed: {e}")
        return []

    async def _parse_single(self, text: str, chunk_id: int = 0, subject_hint: Optional[str] = None) -> List[Dict[str, Any]]:
        """Parse single chunk — uses ONE prompt only (no more 3-prompt loop).

        v4: Cost optimization — removed 3-prompt rotation that tripled API calls
        with minimal benefit (V1/V2/V3 prompts are similar, if V1 fails V2/V3 also fail).
        Per-call cost: 1 prompt × 3 tiers × 2 attempts/tier = 6 calls in the
        happy path. Worst case can grow further if every attempt hits a
        retryable error (timeout / 429 / 5xx) — see _call_gemini for retry
        cadence. (Was 27 in v3.)
        """
        # C2: once a fatal (auth/quota) error occurred, skip the API call so the
        # remaining queued chunks don't keep hammering a dead key/quota.
        if getattr(self, "_fatal_error", None) is not None:
            return []

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
            except GeminiFatalError as e:
                # Record + propagate: this is not recoverable per-chunk.
                self._fatal_error = e
                logger.error(f"Chunk {chunk_id} - FATAL: {e}")
                raise
            except Exception as e:
                logger.error(f"Chunk {chunk_id} - Failed: {e}")

            return []

    async def _call_gemini(self, text: str, prompt_template: str, subject_hint: Optional[str] = None, override_schema=None) -> tuple[List[Dict], str]:
        """Call Gemini API — 2-tier fallback (schema + plain text).

        v5 (B3): bỏ JSON mode trung gian — gần như trùng với Schema mode về
        chi phí/kết quả. Worst-case calls = 2 tiers × 2 attempts = 4 (was 6).
        Plain text giữ lại để cứu các response markdown/text mà schema-mode
        trả rỗng do safety filter / output token tràn.
        """
        from google.genai import types
        from google.genai import errors as genai_errors

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
                    # Treat timeout like a transient server error: retry within
                    # the same tier before falling through. A 90s slowdown on
                    # the first call shouldn't burn the whole tier.
                    wait = (attempt + 1) * 5  # 5/10s
                    logger.warning(f"{label} timed out after 90s (attempt {attempt + 1}), retry in {wait}s")
                    await asyncio.sleep(wait)
                    continue
                except genai_errors.APIError as e:
                    # Typed dispatch by HTTP status code — more reliable than
                    # substring matching on str(e), which misses variants like
                    # google.genai.errors.ClientError without "429" in repr.
                    code = getattr(e, "code", None) or 0
                    if code == 429:
                        wait = (attempt + 1) * 5  # 5/10s
                        logger.warning(f"{label} rate limited (HTTP 429), waiting {wait}s...")
                        await asyncio.sleep(wait)
                        continue
                    if 500 <= code < 600:
                        wait = (attempt + 1) * 15  # 15/30s
                        logger.warning(f"{label} server error (HTTP {code}, attempt {attempt + 1}), retry in {wait}s: {str(e)[:80]}")
                        await asyncio.sleep(wait)
                        continue
                    # Other 4xx (400 invalid request, 401 auth, 403 quota, etc.) —
                    # retrying won't help.
                    logger.warning(f"{label} failed (HTTP {code}): {e}")
                    if code in {400, 401, 403}:
                        raise GeminiFatalError(
                            "Gemini API không khả dụng hoặc GOOGLE_API_KEY không hợp lệ. "
                            "Vui lòng kiểm tra cấu hình API key/quota."
                        ) from e
                    return None, ""
                except Exception as e:
                    # Defensive fallback for non-SDK errors (network blips, DNS,
                    # SSL, ...). Keep the legacy string-match retry on a narrow
                    # set so transient issues still get a second chance.
                    err_str = str(e)
                    err_lower = err_str.lower()
                    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                        wait = (attempt + 1) * 5
                        logger.warning(f"{label} rate limited (untyped), waiting {wait}s...")
                        await asyncio.sleep(wait)
                        continue
                    if (
                        "502" in err_str or "503" in err_str or "500" in err_str
                        or "bad gateway" in err_lower
                        or "unavailable" in err_lower
                        or "server error" in err_lower
                        or "internal error" in err_lower
                    ):
                        wait = (attempt + 1) * 15
                        logger.warning(f"{label} server error (untyped, attempt {attempt + 1}), retry in {wait}s: {err_str[:80]}")
                        await asyncio.sleep(wait)
                        continue
                    logger.warning(f"{label} failed: {e}")
                    return None, ""
            return None, ""

        content = ""
        sys_prompt = self._build_system_prompt(subject_hint)
        tier1_schema = override_schema if override_schema is not None else PARSE_SCHEMA
        # B4: adaptive max_output_tokens dựa trên số câu ước tính trong chunk.
        # Tránh trả tiền buffer 64K khi output thực tế chỉ ~5K.
        # Bump ceiling to 32K (Gemini 2.5 Flash supports 65K output). The
        # legacy 16K cap truncated full-document parses where each question
        # ships with Hướng dẫn chấm + multi-step solution.
        adaptive_max = _adaptive_output_tokens(text, ceiling=min(self.max_tokens, 32768))
        logger.info(
            "_call_gemini: estimated %d questions → max_output_tokens=%d",
            _estimate_question_count(text), adaptive_max,
        )

        # B1: thử dùng Gemini context cache cho system prompt. Nếu cache không
        # khả dụng (prompt < min size, network, etc.) fallback về system_instruction.
        cache_name = await self._get_or_create_cache(sys_prompt)

        # 2-tier fallback: Schema (strict JSON) → Plain text (cứu khi schema bị rỗng)
        for mime, schema, label in [
            ("application/json", tier1_schema, "Schema mode"),
            (None,               None,         "Plain text"),
        ]:
            cfg_kwargs: Dict[str, Any] = dict(
                temperature=0,
                max_output_tokens=adaptive_max,
                safety_settings=[types.SafetySetting(**s) for s in _SAFETY_SETTINGS],
            )
            if cache_name:
                cfg_kwargs["cached_content"] = cache_name
            else:
                cfg_kwargs["system_instruction"] = sys_prompt
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
        """Parallel chunk processing with deduplication + D4 re-queue empty chunks."""
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

        # C2: a fatal (auth/quota) error must abort the whole session — don't
        # fall through to the D4 re-queue, which would burn more quota.
        if getattr(self, "_fatal_error", None) is not None:
            raise self._fatal_error

        # D4: re-queue chunks that returned empty NHƯNG có text dài (>500 chars).
        # Phase 2 chỉ retry 1 lần với prompt nhấn mạnh để tránh loop vô hạn.
        retry_indices = []
        for r in results:
            if isinstance(r, Exception):
                continue
            idx, qs = r
            if not qs and len(chunks[idx].strip()) > 500:
                retry_indices.append(idx)

        if retry_indices:
            logger.warning(
                "D4: re-queue %d empty chunks (indices=%s) with emphatic prompt",
                len(retry_indices), retry_indices,
            )
            retry_tasks = [
                self._parse_single_emphatic(chunks[i], chunk_id=i, subject_hint=subject_hint)
                for i in retry_indices
            ]
            retry_results = await asyncio.gather(*retry_tasks, return_exceptions=True)
            # Update kết quả gốc bằng kết quả retry (chỉ khi retry trả về câu)
            recovered = 0
            for retry_idx, retry_qs in zip(retry_indices, retry_results):
                if isinstance(retry_qs, Exception) or not retry_qs:
                    continue
                # Tìm và replace trong results
                for j, r in enumerate(results):
                    if isinstance(r, Exception):
                        continue
                    if r[0] == retry_idx:
                        results[j] = (retry_idx, retry_qs)
                        recovered += len(retry_qs)
                        break
            logger.info("D4 recovery: extracted %d questions from %d retry chunks",
                        recovered, len(retry_indices))

        sorted_results = sorted(
            [r for r in results if not isinstance(r, Exception)],
            key=lambda x: x[0]
        )

        all_questions: List[Dict] = []
        seen_hashes: set = set()
        merged_count = 0
        for _, questions in sorted_results:
            for q in questions:
                # D5: hash gồm answer + solution để 2 câu cùng đề khác đáp án KHÔNG bị merge
                q_hash = self._hash_question(
                    q.get("question", ""),
                    answer=q.get("answer"),
                    solution=q.get("solution_steps"),
                )
                if q_hash and q_hash not in seen_hashes:
                    seen_hashes.add(q_hash)
                    all_questions.append(q)
                else:
                    merged_count += 1
        if merged_count:
            logger.info(f"D5 dedup: merged {merged_count} duplicate questions")

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

    # ==================== CHUNKING (B6: token-aware) ====================

    # B6 — Token-aware chunking. Gemini Flash tokenize tiếng Việt + LaTeX ~3.5
    # chars/token. Target ~4000 input tokens/chunk → ~14000 chars để giữ buffer
    # an toàn cho system prompt + parse_prompt overhead.
    _CHARS_PER_TOKEN = 3.5  # conservative cho tiếng Việt + LaTeX
    _TARGET_CHUNK_TOKENS = 4000
    _MAX_CHUNK_TOKENS = 6000  # ceiling: vẫn chia chunk nếu question đơn quá dài

    def _target_chunk_chars(self) -> int:
        """Convert target tokens → chars. Tôn trọng `self.max_chunk_size` (override)."""
        token_based = int(self._TARGET_CHUNK_TOKENS * self._CHARS_PER_TOKEN)
        return min(self.max_chunk_size, token_based)

    def _max_chunk_chars(self) -> int:
        token_based = int(self._MAX_CHUNK_TOKENS * self._CHARS_PER_TOKEN)
        return min(self.max_chunk_size, token_based)

    def _smart_chunk(self, text: str) -> List[str]:
        """Smart chunking ưu tiên cắt tại question boundary, target ~4K tokens.

        Strategy:
          1. Tìm tất cả question markers (Câu N, Bài N, N., etc.)
          2. Gom các câu hỏi vào chunk hiện tại cho đến khi vượt `target_chars`
          3. Nếu 1 câu > `max_chars` → fallback `_chunk_by_size` để chia nhỏ
        """
        splits = list(_RE_QUESTION_SPLIT.finditer(text))
        if not splits:
            return self._chunk_by_size(text)

        target_chars = self._target_chunk_chars()
        max_chars = self._max_chunk_chars()
        chunks: List[str] = []
        current_chunk = text[:splits[0].start()]

        for i, match in enumerate(splits):
            start = match.start()
            end = splits[i + 1].start() if i + 1 < len(splits) else len(text)
            question_text = text[start:end]

            # Câu đơn quá dài → flush current rồi chia nhỏ câu này
            if len(question_text) > max_chars:
                if current_chunk.strip():
                    chunks.append(current_chunk)
                # Chia câu quá dài theo size, mỗi sub-chunk vẫn tag là 1 câu
                sub_chunks = self._chunk_by_size(question_text)
                chunks.extend(sub_chunks)
                current_chunk = ""
                continue

            # Gom câu nếu chưa vượt target
            if len(current_chunk) + len(question_text) > target_chars and current_chunk.strip():
                chunks.append(current_chunk)
                current_chunk = question_text
            else:
                current_chunk += question_text

        if current_chunk.strip():
            chunks.append(current_chunk)

        return chunks or [text]

    def _chunk_by_size(self, text: str) -> List[str]:
        """Fallback: chia text theo size, ưu tiên cắt ở markdown heading > \\n\\n > \\n > '. '."""
        target = self._target_chunk_chars()
        chunks: List[str] = []
        pos = 0
        # Ưu tiên separators theo độ rõ ràng (heading > paragraph > sentence)
        separators = ['\n## ', '\n### ', '\n\n', '\n', '. ']
        while pos < len(text):
            end = min(pos + target, len(text))
            chunk = text[pos:end]
            if end < len(text):
                for sep in separators:
                    last_sep = chunk.rfind(sep)
                    if last_sep > target * 0.5:
                        chunk = chunk[:last_sep + len(sep)]
                        break
            chunks.append(chunk)
            pos += len(chunk)
        return chunks

    # ==================== UTILITIES ====================

    def _hash_question(self, text: str, answer: Optional[str] = None,
                       solution: Optional[Any] = None) -> str:
        """D5: hash gồm cả answer + first 50 chars solution để tránh false-merge.

        Trước: chỉ normalize question text → 2 câu cùng đề, khác đáp án bị merge.
        Nay: tag thêm answer + solution prefix.

        Args:
            text: question text (raw)
            answer: question answer (optional, string)
            solution: solution_steps (list or string)
        """
        if not text:
            return ""
        base = _RE_WHITESPACE.sub(' ', text.lower().strip())[:150]
        if answer is None and solution is None:
            return base
        ans_part = (answer or "").strip().lower()[:50]
        sol_text = ""
        if isinstance(solution, list):
            sol_text = " ".join(str(s) for s in solution[:2])
        elif solution is not None:
            sol_text = str(solution)
        sol_part = _RE_WHITESPACE.sub(' ', sol_text.lower().strip())[:50]
        return f"{base}|a={ans_part}|s={sol_part}"

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
        """Extract top-level JSON objects one by one as last resort.

        Walks the string with a brace-depth counter (string-aware) so it works
        for any schema — math questions ({"question": ...}), IELTS passages
        ({"passage_text": ...}, {"parts": ...}), or anything else top-level.
        Objects nested inside another object are skipped; only depth 0→1→0
        transitions count as candidates.

        NOTE: caller (_aggressive_extract_json) already applied _RE_TRIPLE_BACKSLASH fix.
        """
        objects: List[Dict] = []
        depth = 0
        in_string = False
        escape_next = False
        obj_start = -1

        for i, char in enumerate(json_str):
            if escape_next:
                escape_next = False
                continue
            if char == '\\':
                escape_next = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == '{':
                if depth == 0:
                    obj_start = i
                depth += 1
            elif char == '}':
                if depth == 0:
                    continue  # stray closer, skip
                depth -= 1
                if depth == 0 and obj_start >= 0:
                    obj_str = json_str[obj_start:i + 1]
                    obj_str = _RE_TRAILING_COMMA.sub(r'\1', obj_str)
                    obj_str = _RE_CONTROL_CHARS.sub('', obj_str)
                    try:
                        obj = json.loads(obj_str)
                    except json.JSONDecodeError:
                        obj_start = -1
                        continue
                    if isinstance(obj, dict):
                        # Math-question shape — apply math-specific defaults to
                        # preserve prior behavior. Other shapes (e.g. IELTS
                        # passages) pass through untouched so the IELTS pipeline
                        # can recognize them.
                        if "question" in obj:
                            obj.setdefault("type", "TL")
                            obj.setdefault("difficulty", "TH")
                            obj.setdefault("solution_steps", [])
                            obj.setdefault("answer", "")
                            obj.setdefault("grade", None)
                            obj.setdefault("chapter", "")
                            obj.setdefault("lesson_title", "")
                        objects.append(obj)
                    obj_start = -1

        if objects:
            logger.info(f"Extracted {len(objects)} individual objects")
        return objects


# ============ SPEED PRESETS (C1: env-configurable concurrency) ============

def _env_int(name: str, default: int) -> int:
    """Read positive integer env var, fallback to default if invalid."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        val = int(raw)
        return val if val > 0 else default
    except (TypeError, ValueError):
        return default


def create_fast_parser(**kwargs):
    """🚀 Fast: larger chunks, more parallel. Override via GEMINI_CONCURRENCY_FAST."""
    return AIQuestionParser(
        max_chunk_size=20000,
        max_concurrency=_env_int("GEMINI_CONCURRENCY_FAST", _env_int("GEMINI_CONCURRENCY", 5)),
        max_tokens=65536,
        **kwargs,
    )

def create_balanced_parser(**kwargs):
    """⚖️ Balanced. Override via GEMINI_CONCURRENCY_BALANCED or GEMINI_CONCURRENCY."""
    return AIQuestionParser(
        max_chunk_size=15000,
        max_concurrency=_env_int("GEMINI_CONCURRENCY_BALANCED", _env_int("GEMINI_CONCURRENCY", 3)),
        max_tokens=65536,
        **kwargs,
    )

def create_quality_parser(**kwargs):
    """🎯 Quality: smaller chunks, more accurate. Override via GEMINI_CONCURRENCY_QUALITY."""
    return AIQuestionParser(
        max_chunk_size=10000,
        max_concurrency=_env_int("GEMINI_CONCURRENCY_QUALITY", _env_int("GEMINI_CONCURRENCY", 2)),
        max_tokens=65536,
        **kwargs,
    )
