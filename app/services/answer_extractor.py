"""
Answer Extractor — Tìm đáp án từ text OCR, không dùng AI.

3 strategies (ưu tiên theo thứ tự):
1. Answer table: bảng đáp án cuối file (1.A 2.B 3.C ...)
2. Inline answer: đáp án ngay sau câu hỏi (Đáp án: A, ĐA: B, **A**)
3. Answer section: section "Lời giải" / "Hướng dẫn giải"

Confidence scoring:
- table + coverage >= 80% → 0.95
- table + coverage < 80%  → coverage * 0.9
- inline                  → coverage * 0.85
- section                 → 0.7
- none                    → 0.0
"""

import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class AnswerMap:
    answers: dict[int, str] = field(default_factory=dict)
    source: str = "none"       # "table" | "inline" | "section" | "none"
    confidence: float = 0.0


# ── Regex patterns (pre-compiled) ──

# Answer table headers
_RE_ANSWER_HEADER = re.compile(
    r'(?:^|\n)\s*(?:đáp\s*án|answer\s*key|bảng\s*đáp\s*án|phần\s*đáp\s*án|'
    r'ĐÁP\s*ÁN|ANSWER\s*KEY|BẢNG\s*ĐÁP\s*ÁN)\s*[:\.]?\s*\n',
    re.IGNORECASE
)

# Table-style answers: "1.A 2.B" or "1-A 2-B" or "1:A" or "Câu 1: A"
_RE_TABLE_ENTRY = re.compile(
    r'(?:Câu|câu|Bài|bài)?\s*(\d+)\s*[.:\-)\]]\s*([A-Da-d])\b',
    re.IGNORECASE
)

# Dense table: "1A 2B 3C" or "1.A  2.B  3.C" on same line
_RE_DENSE_TABLE = re.compile(
    r'(\d+)\s*[.]?\s*([A-Da-d])(?:\s{1,6}|$)',
)

# Inline answer patterns (after each question)
_RE_INLINE_ANSWER = re.compile(
    r'(?:Đáp\s*án|ĐA|đáp\s*án|Answer|Chọn)\s*[:\s]\s*([A-Da-d])\b',
    re.IGNORECASE
)

# Bold answer: **A** or *A*
_RE_BOLD_ANSWER = re.compile(r'\*\*([A-Da-d])\*\*|\*([A-Da-d])\*')

# Solution section headers
_RE_SOLUTION_HEADER = re.compile(
    r'(?:^|\n)\s*(?:lời\s*giải|hướng\s*dẫn\s*giải|giải|bài\s*giải|'
    r'LỜI\s*GIẢI|HƯỚNG\s*DẪN\s*GIẢI|GIẢI)\s*[:\.]?\s*\n',
    re.IGNORECASE
)

# Question number pattern (for mapping inline answers)
_RE_QUESTION_NUM = re.compile(
    r'(?:Câu|câu|Bài|bài|Question)\s+(\d+)\s*[.:\)]',
    re.IGNORECASE
)


class AnswerExtractor:
    """Extract answers from OCR text using regex strategies."""

    def extract(self, full_text: str, questions: list[dict]) -> AnswerMap:
        """
        Try all strategies and return the best result.

        Args:
            full_text: Full OCR text of the document
            questions: List of dicts with at least {cau_num: int, text: str}

        Returns:
            AnswerMap with answers, source, and confidence
        """
        if not full_text or not questions:
            return AnswerMap()

        total_questions = len(questions)
        question_nums = {q["cau_num"] for q in questions}

        # Strategy 1: Answer table (highest priority)
        table_result = self._extract_from_table(full_text, question_nums)
        if table_result.answers:
            coverage = len(table_result.answers) / max(total_questions, 1)
            if coverage >= 0.8:
                table_result.confidence = 0.95
            else:
                table_result.confidence = coverage * 0.9
            table_result.source = "table"

            if table_result.confidence >= 0.6:
                logger.info(
                    f"Answer table found: {len(table_result.answers)}/{total_questions} "
                    f"answers, confidence={table_result.confidence:.2f}"
                )
                return table_result

        # Strategy 2: Inline answers
        inline_result = self._extract_inline(full_text, questions)
        if inline_result.answers:
            coverage = len(inline_result.answers) / max(total_questions, 1)
            inline_result.confidence = coverage * 0.85
            inline_result.source = "inline"

            if inline_result.confidence >= 0.6:
                logger.info(
                    f"Inline answers found: {len(inline_result.answers)}/{total_questions} "
                    f"answers, confidence={inline_result.confidence:.2f}"
                )
                return inline_result

        # Strategy 3: Solution section
        section_result = self._extract_from_section(full_text, question_nums)
        if section_result.answers:
            section_result.confidence = 0.7
            section_result.source = "section"
            logger.info(
                f"Solution section found: {len(section_result.answers)}/{total_questions} "
                f"answers, confidence={section_result.confidence:.2f}"
            )
            return section_result

        # Merge partial results if individual strategies were below threshold
        if table_result.answers or inline_result.answers:
            merged = AnswerMap()
            merged.answers = {**inline_result.answers, **table_result.answers}  # table wins
            coverage = len(merged.answers) / max(total_questions, 1)
            merged.confidence = coverage * 0.8
            merged.source = "table" if table_result.answers else "inline"
            if merged.confidence >= 0.6:
                logger.info(
                    f"Merged answers: {len(merged.answers)}/{total_questions}, "
                    f"confidence={merged.confidence:.2f}"
                )
                return merged

        logger.info("No answers found in document")
        return AnswerMap()

    def _extract_from_table(self, text: str, question_nums: set[int]) -> AnswerMap:
        """Strategy 1: Find answer table (usually at end of document)."""
        result = AnswerMap()

        # Try to find answer section header first
        header_match = _RE_ANSWER_HEADER.search(text)
        search_text = text[header_match.start():] if header_match else text

        # If no header found, try the last 30% of document (answer tables are at end)
        if not header_match:
            cutoff = int(len(text) * 0.7)
            search_text = text[cutoff:]

        # Extract table entries
        for m in _RE_TABLE_ENTRY.finditer(search_text):
            num = int(m.group(1))
            ans = m.group(2).upper()
            if num in question_nums:
                result.answers[num] = ans

        # Also try dense format on each line
        if len(result.answers) < len(question_nums) * 0.5:
            for line in search_text.split('\n'):
                line = line.strip()
                if not line:
                    continue
                dense_matches = _RE_DENSE_TABLE.findall(line)
                # Only count as dense table if 3+ answers on same line
                if len(dense_matches) >= 3:
                    for num_str, ans in dense_matches:
                        num = int(num_str)
                        if num in question_nums:
                            result.answers[num] = ans.upper()

        return result

    def _extract_inline(self, text: str, questions: list[dict]) -> AnswerMap:
        """Strategy 2: Find answers inline after each question."""
        result = AnswerMap()

        for q in questions:
            q_text = q.get("text", "")
            cau_num = q["cau_num"]

            # Check for inline answer pattern
            m = _RE_INLINE_ANSWER.search(q_text)
            if m:
                result.answers[cau_num] = m.group(1).upper()
                continue

            # Check for bold answer
            m = _RE_BOLD_ANSWER.search(q_text)
            if m:
                ans = (m.group(1) or m.group(2)).upper()
                result.answers[cau_num] = ans
                continue

        return result

    def _extract_from_section(self, text: str, question_nums: set[int]) -> AnswerMap:
        """Strategy 3: Find solution/answer section and extract conclusions."""
        result = AnswerMap()

        header_match = _RE_SOLUTION_HEADER.search(text)
        if not header_match:
            return result

        solution_text = text[header_match.start():]

        # In solution section, look for "Câu X: ... chọn A" patterns
        pattern = re.compile(
            r'(?:Câu|câu|Bài|bài)\s+(\d+)\s*[:.]\s*'
            r'(?:.*?(?:chọn|đáp\s*án|answer)\s*[:\s]*([A-Da-d]))',
            re.IGNORECASE | re.DOTALL
        )

        for m in pattern.finditer(solution_text):
            num = int(m.group(1))
            ans = m.group(2).upper()
            if num in question_nums:
                result.answers[num] = ans

        return result
