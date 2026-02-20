"""AI Question Generator - Sinh de tu ngan hang cau hoi.

Optimizations:
  1. Parallel API calls via asyncio.gather + Semaphore
  2. Structured output via Gemini response_schema (JSON guaranteed)
"""

import os
import re
import json
import asyncio
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# --- Prompt (simplified - schema handles JSON format) ---

GENERATE_PROMPT = """Ban la chuyen gia toan hoc Viet Nam. Sinh {count} cau hoi MOI.

TIEU CHI:
- Dang bai: {q_type}
- Chu de: {topic}
- Do kho: {difficulty}
- Cau hoi KHAC so lieu so voi cau mau nhung GIONG dang bai
- Dap an CHINH XAC, loi giai NGAN GON (toi da 3 buoc)
- LaTeX: dung $...$ va double backslash trong JSON (\\\\frac, \\\\sqrt)

CAU MAU:
{samples}

SINH {count} CAU MOI."""

# --- Gemini structured output schema (OpenAPI 3.0 format) ---

QUESTION_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "question": {
                "type": "STRING",
                "description": "Noi dung cau hoi (co the chua LaTeX)"
            },
            "type": {
                "type": "STRING",
                "description": "TN (trac nghiem) hoac TL (tu luan)"
            },
            "topic": {
                "type": "STRING",
                "description": "Chu de cau hoi"
            },
            "difficulty": {
                "type": "STRING",
                "description": "NB, TH, VD, hoac VDC"
            },
            "answer": {
                "type": "STRING",
                "description": "Dap an day du"
            },
            "solution_steps": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Cac buoc giai (toi da 3)"
            }
        },
        "required": ["question", "type", "topic", "difficulty",
                      "answer", "solution_steps"]
    }
}


class AIQuestionGenerator:

    BATCH_SIZE = 5        # Max questions per API call
    MAX_CONCURRENT = 4    # Max parallel API calls (Gemini rate limit safe)

    def __init__(self):
        self.gemini_api_key = os.getenv("GOOGLE_API_KEY", "")
        self.gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
        self.max_tokens = 32000
        self._client = None
        self._semaphore = None  # Created lazily in running loop
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

    def _get_semaphore(self):
        """Lazy-create semaphore inside the running event loop."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT)
        return self._semaphore

    # ========== PUBLIC API ==========

    async def generate(self, samples, count=5, q_type="TN",
                       topic="Toan", difficulty="TH"):
        """Generate questions - auto-batches and parallelizes if count > BATCH_SIZE."""
        if not self._client:
            raise RuntimeError("AI client not initialized. Check GOOGLE_API_KEY.")

        if count <= self.BATCH_SIZE:
            return await self._generate_single(samples, count, q_type, topic, difficulty)

        return await self._generate_parallel(samples, count, q_type, topic, difficulty)

    async def generate_exam(self, samples, sections, topic="", q_type=""):
        """Generate mixed-difficulty exam - all difficulty levels in PARALLEL."""
        if not self._client:
            raise RuntimeError("AI client not initialized. Check GOOGLE_API_KEY.")

        # Build tasks for each difficulty level
        tasks = []
        task_labels = []
        for section in sections:
            diff = section["difficulty"]
            count = section["count"]
            if count <= 0:
                continue

            # Filter samples by difficulty
            diff_samples = [s for s in samples if s.get("difficulty") == diff]
            if not diff_samples:
                diff_samples = samples[:5]

            tasks.append(self.generate(
                samples=diff_samples,
                count=count,
                q_type=q_type or "TN",
                topic=topic or "Toan",
                difficulty=diff,
            ))
            task_labels.append(f"{count}x{diff}")

        if not tasks:
            return []

        logger.info(f"Exam parallel start: {', '.join(task_labels)}")

        # Run ALL difficulty levels in parallel
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_questions = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Exam section {task_labels[i]} failed: {result}")
            else:
                all_questions.extend(result)
                logger.info(f"Exam section {task_labels[i]}: got {len(result)} questions")

        logger.info(f"Exam total: {len(all_questions)} questions")
        return all_questions

    # ========== PARALLEL BATCHING ==========

    async def _generate_parallel(self, samples, count, q_type, topic, difficulty):
        """Split into batches and run ALL batches in parallel."""
        batches = []
        remaining = count
        while remaining > 0:
            batch_size = min(remaining, self.BATCH_SIZE)
            batches.append(batch_size)
            remaining -= batch_size

        logger.info(f"Parallel: {len(batches)} batches for {count} questions ({q_type}/{difficulty})")

        # Create tasks for all batches
        tasks = [
            self._generate_single(samples, bsize, q_type, topic, difficulty)
            for bsize in batches
        ]

        # Run all batches in parallel (semaphore limits concurrency)
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_questions = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Batch {i+1} failed: {result}")
            else:
                all_questions.extend(result)
                logger.info(f"Batch {i+1}: got {len(result)}/{batches[i]} questions")

        logger.info(f"Parallel total: {len(all_questions)}/{count} questions")
        return all_questions[:count]

    # ========== SINGLE BATCH (with structured output) ==========

    async def _generate_single(self, samples, count, q_type, topic, difficulty):
        """Generate a single batch using structured output."""
        samples_text = self._format_samples(samples)
        prompt = GENERATE_PROMPT
        prompt = prompt.replace("{samples}", samples_text)
        prompt = prompt.replace("{count}", str(count))
        prompt = prompt.replace("{q_type}", q_type)
        prompt = prompt.replace("{topic}", topic)
        prompt = prompt.replace("{difficulty}", difficulty)

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
                "question": self._fix_latex(q.get("question", "")),
                "type": q.get("type", q_type),
                "topic": q.get("topic", topic),
                "difficulty": q.get("difficulty", difficulty),
                "answer": self._fix_latex(q.get("answer", "")),
                "solution_steps": [self._fix_latex(s) for s in q.get("solution_steps", [])],
            })

        logger.info(f"Cleaned: {len(cleaned)} questions")
        return cleaned[:count]

    # ========== GEMINI API CALL (native async + structured output) ==========

    async def _call_gemini(self, prompt):
        """Call Gemini with native async + structured output schema.

        3-tier fallback:
          1. Schema mode (response_schema) - guaranteed valid JSON
          2. JSON mode (response_mime_type only) - usually valid
          3. Plain text - needs manual parsing
        """
        sem = self._get_semaphore()
        async with sem:
            from google.genai import types

            # Tier 1: Structured output with schema
            try:
                response = await self._client.aio.models.generate_content(
                    model=self.gemini_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.7,
                        max_output_tokens=self.max_tokens,
                        response_mime_type="application/json",
                        response_schema=QUESTION_SCHEMA,
                    )
                )
                text = self._safe_text(response)
                if text:
                    return text
            except Exception as e:
                logger.warning(f"Schema mode failed: {e}")

            # Tier 2: JSON mode without schema
            try:
                response = await self._client.aio.models.generate_content(
                    model=self.gemini_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.7,
                        max_output_tokens=self.max_tokens,
                        response_mime_type="application/json",
                    )
                )
                text = self._safe_text(response)
                if text:
                    return text
            except Exception as e:
                logger.warning(f"JSON mode failed: {e}")

            # Tier 3: Plain text
            try:
                response = await self._client.aio.models.generate_content(
                    model=self.gemini_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.7,
                        max_output_tokens=self.max_tokens,
                    )
                )
                return self._safe_text(response)
            except Exception as e:
                raise RuntimeError(f"Gemini API error: {e}")

    # ========== HELPERS ==========

    @staticmethod
    def _safe_text(response):
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
    def _fix_latex(text):
        """Restore control chars that were LaTeX commands."""
        if not text or not isinstance(text, str):
            return text or ""
        text = text.replace('\f', '\\f')
        text = text.replace('\b', '\\b')
        text = text.replace('\r', '\\r')
        text = text.replace('\t', '\\t')
        text = text.replace('\a', '\\a')
        text = text.replace('\v', '\\v')
        return text

    def _format_samples(self, samples):
        if not samples:
            return "(Khong co cau mau)"
        parts = []
        for i, s in enumerate(samples, 1):
            text = s.get("question_text") or s.get("question", "")
            answer = s.get("answer", "")
            parts.append(f"Mau {i}: {text}")
            if answer:
                parts.append(f"  DA: {answer}")
        return "\n".join(parts)

    # ========== JSON PARSING (fallback for non-schema responses) ==========

    def _repair_json(self, text):
        """Fix LaTeX backslashes + truncated JSON."""
        text = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)

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

    def _extract_json(self, text):
        """Extract JSON array with repair for LaTeX + truncation."""
        if not text:
            return []
        text = text.strip()

        if "```" in text:
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

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
            logger.info(f"JSON parsed after full repair ({len(r)} items)")
            return r

        logger.error(f"JSON parse failed. Preview: {text[:200]}...")
        return []

    @staticmethod
    def _try_parse(text):
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
            if isinstance(result, dict) and "questions" in result:
                return result["questions"]
            if isinstance(result, dict):
                return [result]
        except json.JSONDecodeError as e:
            logger.debug(f"JSON parse error at pos {e.pos}: {e.msg}")
        except (ValueError, TypeError):
            pass
        return None


ai_generator = AIQuestionGenerator()