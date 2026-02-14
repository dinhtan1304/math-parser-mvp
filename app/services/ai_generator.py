"""
AI Question Generator - Sinh de tuong tu tu ngan hang cau hoi.
"""

import os
import json
import asyncio
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


GENERATE_PROMPT = """Ban la chuyen gia toan hoc Viet Nam. Nhiem vu: sinh cau hoi toan hoc MOI dua tren cac cau mau.

YEU CAU BAT BUOC:
- Cau hoi KHAC so lieu, ngu canh so voi cau mau, nhung GIONG dang bai va do kho
- Moi cau phai co dap an chinh xac va loi giai chi tiet
- Su dung LaTeX cho cong thuc toan (dung ky hieu $ hoac $$)
- QUAN TRONG: Trong JSON, moi dau \\ trong LaTeX phai viet thanh \\\\ (double backslash)
  Vi du: \\\\frac, \\\\sqrt, \\\\left, \\\\right, \\\\leq, \\\\geq
- Viet bang tieng Viet
- Tra ve DUNG dinh dang JSON array

OUTPUT FORMAT (JSON array):
[
  {
    "question": "Noi dung cau hoi voi LaTeX, vi du: $\\\\frac{a}{b}$",
    "type": "DANG_BAI",
    "topic": "CHU_DE",
    "difficulty": "DO_KHO",
    "answer": "Dap an ngan gon",
    "solution_steps": ["Buoc 1: ...", "Buoc 2: ...", "Buoc 3: ..."]
  }
]

CAU MAU TU NGAN HANG DE (de tham khao dang bai va do kho):
{samples}

HAY SINH CHINH XAC {count} CAU HOI MOI voi dang "{q_type}", chu de "{topic}", do kho "{difficulty}".
Chi tra ve JSON array, khong giai thich them."""


class AIQuestionGenerator:

    def __init__(self):
        self.gemini_api_key = os.getenv("GOOGLE_API_KEY", "")
        self.gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
        self.max_tokens = 16000
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

    async def generate(
        self,
        samples: List[Dict[str, Any]],
        count: int = 5,
        q_type: str = "TN",
        topic: str = "Toan",
        difficulty: str = "TH",
    ) -> List[Dict[str, Any]]:
        if not self._client:
            raise RuntimeError("AI client not initialized. Check GOOGLE_API_KEY.")

        samples_text = self._format_samples(samples)

        prompt = GENERATE_PROMPT
        prompt = prompt.replace("{samples}", samples_text)
        prompt = prompt.replace("{count}", str(count))
        prompt = prompt.replace("{q_type}", q_type)
        prompt = prompt.replace("{topic}", topic)
        prompt = prompt.replace("{difficulty}", difficulty)

        logger.info(f"Calling Gemini: {count} questions, type={q_type}, topic={topic}, diff={difficulty}, samples={len(samples)}")
        raw = await self._call_gemini(prompt)
        logger.info(f"Gemini response length: {len(raw)} chars")

        questions = self._extract_json(raw)
        logger.info(f"Parsed {len(questions)} questions from response")

        cleaned = []
        for q in questions:
            if not isinstance(q, dict):
                continue
            if not q.get("question"):
                continue
            cleaned.append({
                "question": self._fix_latex_chars(q.get("question", "")),
                "type": q.get("type", q_type),
                "topic": q.get("topic", topic),
                "difficulty": q.get("difficulty", difficulty),
                "answer": self._fix_latex_chars(q.get("answer", "")),
                "solution_steps": [self._fix_latex_chars(s) for s in q.get("solution_steps", [])],
            })

        logger.info(f"Generated {len(cleaned)} questions (requested {count})")
        return cleaned[:count]

    @staticmethod
    def _fix_latex_chars(text: str) -> str:
        """Restore control characters that were actually LaTeX commands."""
        if not text or not isinstance(text, str):
            return text or ""
        text = text.replace('\f', '\\f')
        text = text.replace('\b', '\\b')
        text = text.replace('\r', '\\r')
        text = text.replace('\t', '\\t')
        return text

    def _format_samples(self, samples: List[Dict]) -> str:
        if not samples:
            return "(Khong co cau mau - hay sinh cau hoi tu dau)"
        parts = []
        for i, s in enumerate(samples, 1):
            text = s.get("question_text") or s.get("question", "")
            answer = s.get("answer", "")
            steps = s.get("solution_steps", [])
            if isinstance(steps, str):
                try:
                    steps = json.loads(steps)
                except (json.JSONDecodeError, TypeError):
                    steps = []
            part = f"--- Cau mau {i} ---\nCau hoi: {text}\n"
            if answer:
                part += f"Dap an: {answer}\n"
            if steps:
                part += "Loi giai:\n"
                for j, step in enumerate(steps, 1):
                    part += f"  {j}. {step}\n"
            parts.append(part)
        return "\n".join(parts)

    async def _call_gemini(self, prompt: str) -> str:
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
                return self._safe_response_text(response)
            except Exception as e:
                logger.warning(f"Gemini JSON mode failed: {e}, retrying plain")
                try:
                    response = self._client.models.generate_content(
                        model=self.gemini_model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            temperature=0.7,
                            max_output_tokens=self.max_tokens,
                        )
                    )
                    return self._safe_response_text(response)
                except Exception as e2:
                    raise RuntimeError(f"Gemini API error: {e2}")
        return await loop.run_in_executor(None, call_api)

    @staticmethod
    def _safe_response_text(response) -> str:
        try:
            if hasattr(response, 'text') and response.text:
                return response.text
        except Exception:
            pass
        try:
            for candidate in response.candidates:
                for part in candidate.content.parts:
                    if hasattr(part, 'text') and part.text:
                        return part.text
        except Exception:
            pass
        raw = str(response)
        logger.warning(f"Fallback str(response): {raw[:200]}")
        return raw

    def _repair_latex_json(self, text: str) -> str:
        """Fix LaTeX backslashes that break JSON parsing.
        
        \\frac -> invalid JSON escape \\f
        Fix: double any backslash NOT followed by valid JSON escape char.
        """
        import re
        # Double backslashes NOT followed by valid JSON escape chars
        fixed = re.sub(r'\\(?!["\\\\/bfnrtu])', r'\\\\', text)
        return fixed

    def _extract_json(self, text: str) -> List[Dict]:
        """Extract JSON array from response with LaTeX repair."""
        if not text:
            return []

        text = text.strip()

        # Remove markdown fences
        if "```" in text:
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        # Attempt 1: Direct parse
        parsed = self._try_parse(text)
        if parsed is not None:
            return parsed

        # Attempt 2: Extract [...] subset
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            subset = text[start:end + 1]

            parsed = self._try_parse(subset)
            if parsed is not None:
                return parsed

            # Attempt 3: Repair LaTeX then parse
            repaired = self._repair_latex_json(subset)
            parsed = self._try_parse(repaired)
            if parsed is not None:
                logger.info("JSON parsed after LaTeX backslash repair")
                return parsed

        # Attempt 4: Repair full text
        repaired = self._repair_latex_json(text)
        parsed = self._try_parse(repaired)
        if parsed is not None:
            logger.info("JSON parsed after full repair")
            return parsed

        logger.error(f"All JSON parse attempts failed. Preview: {text[:300]}...")
        return []

    @staticmethod
    def _try_parse(text: str):
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
            if isinstance(result, dict) and "questions" in result:
                return result["questions"]
            if isinstance(result, dict):
                return [result]
            return None
        except (json.JSONDecodeError, ValueError):
            return None


ai_generator = AIQuestionGenerator()