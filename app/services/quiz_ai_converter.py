"""
Quiz AI Converter — uses Gemini to generate structured quiz data
when converting bank questions to specific quiz types.

Handles:
  - Generating distractor choices for multiple_choice
  - Generating checkbox data (multiple correct answers)
  - Converting questions to true/false statements
  - Generating reorder items from question content
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class QuizAIConverter:
    """Singleton service for AI-powered quiz question conversion."""

    MAX_CONCURRENT = 5

    def __init__(self):
        self.gemini_api_key = os.getenv("GOOGLE_API_KEY", "")
        self.gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self._client = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._init_client()

    def _init_client(self):
        if not self.gemini_api_key:
            logger.warning("No GOOGLE_API_KEY for QuizAIConverter")
            return
        try:
            from google import genai
            self._client = genai.Client(api_key=self.gemini_api_key)
            logger.info("QuizAIConverter: Gemini client initialized")
        except ImportError:
            logger.error("google-genai not installed")
        except Exception as e:
            logger.error(f"QuizAIConverter init error: {e}")

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT)
        return self._semaphore

    async def _call_gemini(self, prompt: str, temperature: float = 0.6) -> Optional[str]:
        """Call Gemini and return raw text response."""
        if not self._client:
            return None
        try:
            from google.genai import types
            async with self._get_semaphore():
                response = await asyncio.wait_for(
                    self._client.aio.models.generate_content(
                        model=self.gemini_model,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            temperature=temperature,
                            max_output_tokens=2048,
                            response_mime_type="application/json",
                        ),
                    ),
                    timeout=30,
                )
                return response.text
        except asyncio.TimeoutError:
            logger.warning("QuizAIConverter: Gemini timeout")
            return None
        except Exception as e:
            logger.warning(f"QuizAIConverter: Gemini error: {e}")
            return None

    def _parse_json(self, text: Optional[str]) -> Optional[Any]:
        """Parse JSON from Gemini response, handling markdown fences."""
        if not text:
            return None
        # Strip markdown code fences
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning(f"QuizAIConverter: JSON parse failed: {text[:200]}")
            return None

    # ─── Public Methods ──────────────────────────────────────────

    async def generate_choices(
        self,
        question_text: str,
        correct_answer: str,
        count: int = 3,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Generate distractor choices for multiple_choice.
        Returns list of dicts: [{"key":"B","text":"..."},...]
        """
        prompt = f"""Bạn là giáo viên Toán. Cho câu hỏi và đáp án đúng, hãy tạo {count} đáp án sai (distractors) hợp lý.
Đáp án sai phải có độ khó tương đương, dễ nhầm lẫn nhưng SAI.

Câu hỏi: {question_text}
Đáp án đúng: {correct_answer}

Trả về JSON array gồm {count} đáp án sai, mỗi đáp án là một string:
["đáp án sai 1", "đáp án sai 2", "đáp án sai 3"]"""

        text = await self._call_gemini(prompt)
        data = self._parse_json(text)
        if not isinstance(data, list) or len(data) == 0:
            return None

        keys = "BCDEFGH"
        choices = []
        for i, item in enumerate(data[:count]):
            choices.append({
                "key": keys[i] if i < len(keys) else chr(66 + i),
                "text": str(item),
                "is_correct": False,
                "media": None,
            })
        return choices

    async def generate_true_false(
        self,
        question_text: str,
        answer: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """
        Convert a question into a true/false statement.
        Returns: {"statement": "...", "answer": true/false}
        """
        prompt = f"""Bạn là giáo viên. Chuyển câu hỏi sau thành một PHÁT BIỂU ĐÚNG hoặc SAI.

Câu hỏi gốc: {question_text}
{f'Đáp án gốc: {answer}' if answer else ''}

Trả về JSON:
{{"statement": "phát biểu đúng hoặc sai", "answer": true hoặc false}}

Phát biểu phải rõ ràng, ngắn gọn. answer=true nếu phát biểu đúng, answer=false nếu sai."""

        text = await self._call_gemini(prompt)
        data = self._parse_json(text)
        if not isinstance(data, dict) or "statement" not in data:
            return None
        return {
            "statement": str(data["statement"]),
            "answer": bool(data.get("answer", True)),
        }

    async def generate_reorder_items(
        self,
        question_text: str,
        answer: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """
        Generate reorder items from question content.
        Returns: {"question": "...", "items": [{"id":"I1","text":"..."},...], "correct_order": ["I1","I3",...]}
        """
        prompt = f"""Bạn là giáo viên. Tạo bài tập SẮP XẾP THỨ TỰ từ nội dung câu hỏi sau.

Câu hỏi gốc: {question_text}
{f'Đáp án gốc: {answer}' if answer else ''}

Tạo 4-6 bước/phần cần sắp xếp theo thứ tự đúng. Trả về JSON:
{{
  "question": "Sắp xếp các bước sau theo thứ tự đúng:",
  "items": [
    {{"id": "I1", "text": "bước/phần 1"}},
    {{"id": "I2", "text": "bước/phần 2"}},
    {{"id": "I3", "text": "bước/phần 3"}},
    {{"id": "I4", "text": "bước/phần 4"}}
  ],
  "correct_order": ["I1", "I2", "I3", "I4"]
}}

items phải liệt kê theo thứ tự XÁO TRỘN (không phải thứ tự đúng).
correct_order là thứ tự ĐÚNG."""

        text = await self._call_gemini(prompt)
        data = self._parse_json(text)
        if not isinstance(data, dict) or "items" not in data:
            return None

        items = []
        for item in data.get("items", []):
            if isinstance(item, dict) and "id" in item and "text" in item:
                items.append({"id": str(item["id"]), "text": str(item["text"])})

        if len(items) < 2:
            return None

        correct_order = [str(x) for x in data.get("correct_order", [])]
        return {
            "question": str(data.get("question", question_text)),
            "items": items,
            "correct_order": correct_order,
        }

    async def generate_checkbox_data(
        self,
        question_text: str,
        answer: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """
        Generate choices for checkbox (multiple correct answers).
        Returns: {"choices": [...], "correct_keys": ["A","C"]}
        """
        prompt = f"""Bạn là giáo viên. Tạo câu hỏi NHIỀU ĐÁP ÁN ĐÚNG từ nội dung sau.

Câu hỏi gốc: {question_text}
{f'Đáp án gốc: {answer}' if answer else ''}

Tạo 4-6 đáp án, trong đó có 2-3 đáp án ĐÚNG. Trả về JSON:
{{
  "choices": [
    {{"key": "A", "text": "đáp án 1"}},
    {{"key": "B", "text": "đáp án 2"}},
    {{"key": "C", "text": "đáp án 3"}},
    {{"key": "D", "text": "đáp án 4"}}
  ],
  "correct_keys": ["A", "C"]
}}"""

        text = await self._call_gemini(prompt)
        data = self._parse_json(text)
        if not isinstance(data, dict) or "choices" not in data:
            return None

        choices = []
        correct_keys = [str(k) for k in data.get("correct_keys", [])]
        for c in data.get("choices", []):
            if isinstance(c, dict) and "key" in c and "text" in c:
                key = str(c["key"])
                choices.append({
                    "key": key,
                    "text": str(c["text"]),
                    "is_correct": key in correct_keys,
                    "media": None,
                })

        if len(choices) < 2:
            return None

        return {
            "choices": choices,
            "correct_keys": correct_keys,
        }


# Module-level singleton
quiz_ai_converter = QuizAIConverter()
