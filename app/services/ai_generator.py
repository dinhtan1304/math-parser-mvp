"""AI Question Generator - Sinh de tu ngan hang cau hoi."""

import os
import re
import json
import asyncio
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

GENERATE_PROMPT = """Ban la chuyen gia toan hoc Viet Nam. Sinh {count} cau hoi MOI.

TIEU CHI:
- Dang bai: {q_type}
- Chu de: {topic}
- Do kho: {difficulty}
- Cau hoi KHAC so lieu so voi cau mau nhung GIONG dang bai
- Dap an CHINH XAC, loi giai NGAN GON (toi da 3 buoc)
- LaTeX: dung $...$ va trong JSON phai double backslash (\\\\frac, \\\\sqrt)

JSON FORMAT:
[{{"question":"...","type":"...","topic":"...","difficulty":"...","answer":"...","solution_steps":["B1:...","B2:..."]}}]

CAU MAU:
{samples}

SINH {count} CAU MOI. Chi tra ve JSON."""


class AIQuestionGenerator:

    def __init__(self):
        self.gemini_api_key = os.getenv("GOOGLE_API_KEY", "")
        self.gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
        self.max_tokens = 32000
        self._client = None
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

    BATCH_SIZE = 5  # Max questions per Gemini call to avoid truncation

    async def generate(self, samples, count=5, q_type="TN",
                       topic="Toan", difficulty="TH"):
        if not self._client:
            raise RuntimeError("AI client not initialized. Check GOOGLE_API_KEY.")

        # Batch if count > BATCH_SIZE
        if count > self.BATCH_SIZE:
            return await self._generate_batched(samples, count, q_type, topic, difficulty)

        return await self._generate_single(samples, count, q_type, topic, difficulty)

    async def _generate_batched(self, samples, count, q_type, topic, difficulty):
        """Split large requests into batches."""
        all_questions = []
        remaining = count
        batch_num = 0

        while remaining > 0:
            batch_num += 1
            batch_size = min(remaining, self.BATCH_SIZE)
            logger.info(f"Batch {batch_num}: generating {batch_size} questions ({len(all_questions)}/{count} done)")

            try:
                questions = await self._generate_single(
                    samples, batch_size, q_type, topic, difficulty
                )
                all_questions.extend(questions)
                remaining -= len(questions)

                # If AI returned fewer than requested, don't infinite loop
                if len(questions) < batch_size:
                    remaining -= (batch_size - len(questions))

            except Exception as e:
                logger.error(f"Batch {batch_num} failed: {e}")
                remaining -= batch_size  # skip and continue

        logger.info(f"Batched total: {len(all_questions)}/{count} questions")
        return all_questions[:count]

    async def _generate_single(self, samples, count, q_type, topic, difficulty):
        """Generate a single batch of questions."""

        samples_text = self._format_samples(samples)
        prompt = GENERATE_PROMPT
        prompt = prompt.replace("{samples}", samples_text)
        prompt = prompt.replace("{count}", str(count))
        prompt = prompt.replace("{q_type}", q_type)
        prompt = prompt.replace("{topic}", topic)
        prompt = prompt.replace("{difficulty}", difficulty)

        logger.info(f"Generating {count} questions: {q_type}/{topic}/{difficulty} ({len(samples)} samples)")
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

    async def generate_exam(self, samples, sections, topic="", q_type=""):
        """Generate a mixed-difficulty exam by calling AI per difficulty level.

        This avoids response truncation by keeping each call small.
        """
        if not self._client:
            raise RuntimeError("AI client not initialized. Check GOOGLE_API_KEY.")

        all_questions = []

        for section in sections:
            diff = section["difficulty"]
            count = section["count"]
            if count <= 0:
                continue

            logger.info(f"Exam section: {count} x {diff}")

            # Filter samples by difficulty if possible
            diff_samples = [s for s in samples if s.get("difficulty") == diff]
            if not diff_samples:
                diff_samples = samples[:5]

            try:
                questions = await self.generate(
                    samples=diff_samples,
                    count=count,
                    q_type=q_type or "TN",
                    topic=topic or "Toan",
                    difficulty=diff,
                )
                all_questions.extend(questions)
                logger.info(f"  Got {len(questions)} {diff} questions")
            except Exception as e:
                logger.error(f"  Failed for {diff}: {e}")

        logger.info(f"Exam total: {len(all_questions)} questions")
        return all_questions

    @staticmethod
    def _fix_latex(text):
        """Restore control chars that were LaTeX commands."""
        if not text or not isinstance(text, str):
            return text or ""
        text = text.replace('\f', '\\f')   # \frac, \forall
        text = text.replace('\b', '\\b')   # \begin, \binom
        text = text.replace('\r', '\\r')   # \right, \rangle
        text = text.replace('\t', '\\t')   # \text, \times, \theta
        text = text.replace('\a', '\\a')   # \alpha, \approx
        text = text.replace('\v', '\\v')   # \vec, \varphi
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

    async def _call_gemini(self, prompt):
        loop = asyncio.get_running_loop()
        def call_api():
            from google.genai import types
            try:
                response = self._client.models.generate_content(
                    model=self.gemini_model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.7,
                        max_output_tokens=self.max_tokens,
                        response_mime_type="application/json",
                    )
                )
                return self._safe_text(response)
            except Exception as e:
                logger.warning(f"JSON mode failed: {e}, retrying plain")
                try:
                    response = self._client.models.generate_content(
                        model=self.gemini_model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            temperature=0.7,
                            max_output_tokens=self.max_tokens,
                        )
                    )
                    return self._safe_text(response)
                except Exception as e2:
                    raise RuntimeError(f"Gemini API error: {e2}")
        return await loop.run_in_executor(None, call_api)

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
        return str(response)

    def _repair_json(self, text):
        """Fix LaTeX backslashes + truncated JSON."""
        # Step 1: Fix invalid escape sequences from LaTeX
        text = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)

        # Step 2: Try direct parse first
        try:
            json.loads(text)
            return text
        except (json.JSONDecodeError, ValueError):
            pass

        # Step 3: Handle truncation - find last complete JSON object
        # Strategy: find the last "}," or "}" and close the array there
        # This drops the truncated last item but keeps all complete ones

        # Find array start
        arr_start = text.find('[')
        if arr_start == -1:
            return text

        # Find all positions of complete objects ("},")
        last_complete = -1
        depth = 0
        i = arr_start + 1
        while i < len(text):
            ch = text[i]
            if ch == '"':
                # Skip string content
                i += 1
                while i < len(text) and text[i] != '"':
                    if text[i] == '\\':
                        i += 1  # skip escaped char
                    i += 1
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    last_complete = i
            i += 1

        if last_complete > arr_start:
            # Cut at last complete object and close array
            result = text[:last_complete + 1] + ']'
            return result

        return text

    def _extract_json(self, text):
        """Extract JSON array with repair for LaTeX + truncation."""
        if not text:
            return []
        text = text.strip()

        # Remove markdown fences
        if "```" in text:
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        # Attempt 1: Direct parse
        r = self._try_parse(text)
        if r is not None:
            return r

        # Attempt 2: Extract [...] then parse
        start = text.find("[")
        if start != -1:
            subset = text[start:]
            end = subset.rfind("]")
            if end != -1:
                r = self._try_parse(subset[:end + 1])
                if r is not None:
                    return r

            # Attempt 3: Repair (LaTeX + truncation) then parse
            repaired = self._repair_json(subset)
            r = self._try_parse(repaired)
            if r is not None:
                logger.info(f"JSON parsed after repair ({len(r)} items)")
                return r

        # Attempt 4: Full text repair
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