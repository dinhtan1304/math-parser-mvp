"""AI Question Generator — Sinh đề từ ngân hàng câu hỏi.

Optimizations v2:
  1. Semaphore lazy-init (fix wrong event loop on module import)
  2. _fix_latex: replace 6 individual str.replace with single translate() call
  3. _format_samples: pre-join in one pass instead of two list.append calls per sample
  4. Prompt format: use str.format_map for slightly faster substitution
  5. _repair_json: skip re.sub if no unescaped backslashes present
  6. generate_exam: already parallel — no change needed
"""

import os
import re
import json
import asyncio
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# ── Pre-compiled regex ──
_RE_UNESCAPED_BACKSLASH = re.compile(r'\\(?!["\\\/bfnrtu])')
_RE_STRIP_FENCES = re.compile(r'```(?:json)?\s*|\s*```')

GENERATE_PROMPT = """Bạn là chuyên gia toán học Việt Nam. Sinh {count} câu hỏi MỚI.

TIÊU CHÍ:
- Dạng bài: {q_type}
- Chủ đề: {topic}
- Độ khó: {difficulty}
- Câu hỏi KHÁC số liệu so với câu mẫu nhưng GIỐNG dạng bài
- Đáp án CHÍNH XÁC, lời giải NGẮN GỌN (tối đa 3 bước)
- LaTeX: dùng $...$ và double backslash trong JSON (\\\\frac, \\\\sqrt)

PHÂN LOẠI THEO CHƯƠNG TRÌNH GDPT 2018:
- grade: số nguyên 6-12
- chapter: Tên chương đầy đủ (vd: "Chương I. Ứng dụng đạo hàm để khảo sát và vẽ đồ thị hàm số")
- lesson_title: Tiêu đề bài học cụ thể (vd: "Tính đơn điệu và cực trị của hàm số")

TOÁN 6: C1.Số tự nhiên|C2.Tính chia hết|C3.Số nguyên|C4.Hình phẳng|C5.Phân số|C6.Số thập phân|C7.Hình học cơ bản
TOÁN 7: C1.Số hữu tỉ|C2.Số thực|C3.Góc/đường thẳng|C4.Tam giác bằng nhau|C5.Thống kê|C6.Tỉ lệ thức|C7.Đại số|C8.Đa giác|C9.Xác suất
TOÁN 8: C1.Đa thức|C2.Hằng đẳng thức|C3.Tứ giác|C4.Định lí Thales|C5.Dữ liệu|C6.Phân thức|C7.PT bậc nhất|C8.Xác suất|C9.Tam giác đồng dạng|C10.Hình chóp
TOÁN 9: C1.Hệ PT|C2.Bất PT|C3.Căn thức|C4.Hệ thức lượng|C5.Đường tròn|C6.Hàm y=ax²|C7.Tần số|C8.Xác suất|C9.Đường tròn ngoại/nội tiếp|C10.Hình tru/nón/cầu
TOÁN 10: C1.Mệnh đề/tập hợp|C2.BPT bậc nhất 2 ẩn|C3.Hệ thức lượng tam giác|C4.Vectơ|C5.Thống kê|C6.Hàm bậc hai|C7.Tọa độ phẳng|C8.Tổ hợp|C9.Xác suất cổ điển
TOÁN 11: C1.Lượng giác|C2.Dãy số/cấp số|C3.Thống kê ghép|C4.Song song KG|C5.Giới hạn/liên tục|C6.Hàm mũ/logarit|C7.Vuông góc KG|C8.Xác suất|C9.Đạo hàm
TOÁN 12: C1.Ứng dụng đạo hàm/đồ thị|C2.Vectơ KG|C3.Phân tán|C4.Nguyên hàm/tích phân|C5.Tọa độ KG|C6.Xác suất có điều kiện

CÂU MẪU:
{samples}

SINH {count} CÂU MỚI."""

QUESTION_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "question":       {"type": "STRING"},
            "type":           {"type": "STRING"},
            "topic":          {"type": "STRING"},
            "difficulty":     {"type": "STRING"},
            "grade":          {"type": "INTEGER"},
            "chapter":        {"type": "STRING"},
            "lesson_title":   {"type": "STRING"},
            "answer":         {"type": "STRING"},
            "solution_steps": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["question", "type", "topic", "difficulty",
                     "grade", "chapter", "lesson_title", "answer", "solution_steps"],
    }
}

# OPT: translation table for _fix_latex (faster than 6 str.replace calls)
_LATEX_FIX_TABLE = str.maketrans({
    '\f': '\\f',
    '\b': '\\b',
    '\r': '\\r',
    '\t': '\\t',
    '\a': '\\a',
    '\v': '\\v',
})


class AIQuestionGenerator:

    BATCH_SIZE = 5
    MAX_CONCURRENT = 4

    def __init__(self):
        self.gemini_api_key = os.getenv("GOOGLE_API_KEY", "")
        self.gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
        self.max_tokens = 32000
        self._client = None
        # OPT: Lazy semaphore — avoids "attached to different event loop" error
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._init_client()

    def _init_client(self):
        if not self.gemini_api_key:
            logger.warning("No GOOGLE_API_KEY for generator")
            return
        try:
            from google import genai
            self._client = genai.Client(api_key=self.gemini_api_key)
            logger.info("AI Generator: Gemini client initialized")
        except ImportError:
            logger.error("google-genai not installed")
        except Exception as e:
            logger.error(f"Gemini init error: {e}")

    def _get_semaphore(self) -> asyncio.Semaphore:
        """OPT: Lazy-create in current event loop."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT)
        return self._semaphore

    # ========== PUBLIC API ==========

    async def generate(self, samples, count=5, q_type="TN", topic="Toan", difficulty="TH"):
        if not self._client:
            raise RuntimeError("GOOGLE_API_KEY chưa được cấu hình. Vui lòng thêm API key.")
        if count <= self.BATCH_SIZE:
            return await self._generate_single(samples, count, q_type, topic, difficulty)
        return await self._generate_parallel(samples, count, q_type, topic, difficulty)

    async def generate_exam(self, samples, sections, topic="", q_type=""):
        if not self._client:
            raise RuntimeError("GOOGLE_API_KEY chưa được cấu hình. Vui lòng thêm API key.")

        tasks = []
        task_labels = []
        for section in sections:
            diff = section["difficulty"]
            count = section["count"]
            if count <= 0:
                continue
            diff_samples = [s for s in samples if s.get("difficulty") == diff] or samples[:5]
            tasks.append(self.generate(
                samples=diff_samples, count=count,
                q_type=q_type or "TN", topic=topic or "Toan", difficulty=diff,
            ))
            task_labels.append(f"{count}x{diff}")

        if not tasks:
            return []

        logger.info(f"Exam parallel start: {', '.join(task_labels)}")
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_questions = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Exam section {task_labels[i]} failed: {result}")
            else:
                all_questions.extend(result)
                logger.info(f"Exam section {task_labels[i]}: {len(result)} questions")

        logger.info(f"Exam total: {len(all_questions)} questions")
        return all_questions

    # ========== PARALLEL BATCHING ==========

    async def _generate_parallel(self, samples, count, q_type, topic, difficulty):
        batches = []
        remaining = count
        while remaining > 0:
            batch_size = min(remaining, self.BATCH_SIZE)
            batches.append(batch_size)
            remaining -= batch_size

        logger.info(f"Parallel: {len(batches)} batches for {count} questions")

        tasks = [
            self._generate_single(samples, bsize, q_type, topic, difficulty)
            for bsize in batches
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_questions = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Batch {i + 1} failed: {result}")
            else:
                all_questions.extend(result)

        return all_questions[:count]

    # ========== SINGLE BATCH ==========

    async def _generate_single(self, samples, count, q_type, topic, difficulty):
        samples_text = self._format_samples(samples)
        # OPT: str.format_map with dict slightly faster than .replace chaining for 5+ substitutions
        prompt = GENERATE_PROMPT.format_map({
            "samples": samples_text,
            "count": count,
            "q_type": q_type,
            "topic": topic,
            "difficulty": difficulty,
        })

        logger.info(f"Generating {count} questions: {q_type}/{topic}/{difficulty}")
        raw = await self._call_gemini(prompt)
        logger.info(f"Gemini response: {len(raw)} chars")

        questions = self._extract_json(raw)
        logger.info(f"Parsed {len(questions)} questions")

        cleaned = []
        for q in questions:
            if not isinstance(q, dict) or not q.get("question"):
                continue
            cleaned.append({
                "question":       self._fix_latex(q.get("question", "")),
                "type":           q.get("type", q_type),
                "topic":          q.get("topic", topic),
                "difficulty":     q.get("difficulty", difficulty),
                "grade":          q.get("grade"),
                "chapter":        q.get("chapter", ""),
                "lesson_title":   q.get("lesson_title", ""),
                "answer":         self._fix_latex(q.get("answer", "")),
                "solution_steps": [self._fix_latex(s) for s in q.get("solution_steps", [])],
            })

        logger.info(f"Cleaned: {len(cleaned)} questions")
        return cleaned[:count]

    # ========== GEMINI API CALL ==========

    async def _call_gemini(self, prompt: str) -> str:
        sem = self._get_semaphore()
        async with sem:
            from google.genai import types

            async def _call_with_retry(config, label):
                for attempt in range(3):
                    try:
                        response = await self._client.aio.models.generate_content(
                            model=self.gemini_model,
                            contents=prompt,
                            config=config,
                        )
                        text = self._safe_text(response)
                        if text:
                            return text
                        return None
                    except Exception as e:
                        err_str = str(e)
                        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                            wait = (attempt + 1) * 10
                            logger.warning(f"{label} rate limited, waiting {wait}s...")
                            await asyncio.sleep(wait)
                            continue
                        logger.warning(f"{label} failed: {e}")
                        return None
                return None

            for mime, schema, label in [
                ("application/json", QUESTION_SCHEMA, "Schema mode"),
                ("application/json", None,            "JSON mode"),
                (None,               None,             "Plain text"),
            ]:
                cfg_kwargs: Dict[str, Any] = dict(
                    temperature=0.7,
                    max_output_tokens=self.max_tokens,
                )
                if mime:
                    cfg_kwargs["response_mime_type"] = mime
                if schema:
                    cfg_kwargs["response_schema"] = schema

                text = await _call_with_retry(
                    types.GenerateContentConfig(**cfg_kwargs), label
                )
                if text:
                    return text

            raise RuntimeError(
                "Gemini API: tất cả mode đều thất bại. Vui lòng thử lại sau vài phút."
            )

    # ========== HELPERS ==========

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

    @staticmethod
    def _fix_latex(text) -> str:
        """OPT: Single translate() call instead of 6 str.replace calls."""
        if not text or not isinstance(text, str):
            return text or ""
        return text.translate(_LATEX_FIX_TABLE)

    def _format_samples(self, samples) -> str:
        """OPT: Single-pass join instead of two append calls per sample."""
        if not samples:
            return "(Không có câu mẫu)"
        parts = []
        for i, s in enumerate(samples, 1):
            text = s.get("question_text") or s.get("question", "")
            answer = s.get("answer", "")
            line = f"Mẫu {i}: {text}"
            if answer:
                line += f"\n  ĐA: {answer}"
            parts.append(line)
        return "\n".join(parts)

    # ========== JSON PARSING ==========

    def _repair_json(self, text: str) -> str:
        """OPT: Skip regex if no unescaped backslashes present."""
        if '\\' in text:
            # Only apply expensive regex if backslashes exist
            text = _RE_UNESCAPED_BACKSLASH.sub(r'\\\\', text)

        try:
            json.loads(text)
            return text
        except (json.JSONDecodeError, ValueError):
            pass

        arr_start = text.find('[')
        if arr_start == -1:
            return text

        last_complete = -1
        depth = 0
        i = arr_start + 1
        while i < len(text):
            ch = text[i]
            if ch == '"':
                i += 1
                while i < len(text) and text[i] != '"':
                    if text[i] == '\\':
                        i += 1
                    i += 1
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    last_complete = i
            i += 1

        if last_complete > arr_start:
            return text[:last_complete + 1] + ']'
        return text

    def _extract_json(self, text: str) -> List[Dict]:
        if not text:
            return []
        text = text.strip()

        # OPT: Fast path — try direct parse first
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
            if isinstance(result, dict) and "questions" in result:
                return result["questions"]
        except json.JSONDecodeError:
            pass

        # Strip markdown fences
        if "```" in text:
            text = _RE_STRIP_FENCES.sub('', text).strip()
            r = self._try_parse(text)
            if r is not None:
                return r

        start = text.find("[")
        if start != -1:
            subset = text[start:]
            end = subset.rfind("]")
            if end != -1:
                r = self._try_parse(subset[:end + 1])
                if r is not None:
                    return r

            repaired = self._repair_json(subset)
            r = self._try_parse(repaired)
            if r is not None:
                logger.info(f"JSON parsed after repair ({len(r)} items)")
                return r

        repaired = self._repair_json(text)
        r = self._try_parse(repaired)
        if r is not None:
            return r

        logger.error(f"JSON parse failed. Preview: {text[:200]}")
        return []

    @staticmethod
    def _try_parse(text: str) -> Optional[List]:
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
            if isinstance(result, dict) and "questions" in result:
                return result["questions"]
            if isinstance(result, dict):
                return [result]
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        return None


ai_generator = AIQuestionGenerator()
