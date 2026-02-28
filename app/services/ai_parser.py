"""
AI Question Parser - Phân tích đề toán bằng Gemini API
Optimized for JSON output with parallel processing

Output format:
[
  {
    "question": "...",
    "type": "...",
    "topic": "...",
    "difficulty": "...",
    "grade": 12,
    "chapter": "...",
    "lesson_title": "...",
    "solution_steps": [],
    "answer": "..."
  }
]
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
    CLAUDE = "claude"
    GEMINI = "gemini"
    AUTO = "auto"


# ── Structured output schema (Sprint 2, Task 10) ──
# Gemini response_schema guarantees valid JSON matching this structure.
# Eliminates ~90% of JSON repair logic.
PARSE_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "question": {
                "type": "STRING",
                "description": "Full question text with LaTeX math notation"
            },
            "type": {
                "type": "STRING",
                "description": "TL, TN, Rut gon bieu thuc, So sanh, Chung minh, Tim GTNN, Giai phuong trinh"
            },
            "topic": {
                "type": "STRING",
                "description": "Curriculum topic name"
            },
            "difficulty": {
                "type": "STRING",
                "description": "NB, TH, VD, or VDC"
            },
            "grade": {
                "type": "INTEGER",
                "description": "Grade level 6-12 (lop 6 den lop 12)"
            },
            "chapter": {
                "type": "STRING",
                "description": "Chapter name e.g. Chuong I. Ung dung dao ham de khao sat va ve do thi ham so"
            },
            "lesson_title": {
                "type": "STRING",
                "description": "Lesson title e.g. Tinh don dieu va cuc tri cua ham so"
            },
            "answer": {
                "type": "STRING",
                "description": "Final answer with LaTeX if needed"
            },
            "solution_steps": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "Step-by-step solution with LaTeX"
            },
        },
        "required": ["question", "type", "topic", "difficulty",
                      "grade", "chapter", "lesson_title",
                      "answer", "solution_steps"],
    }
}


class AIQuestionParser:
    """
    Parser sử dụng Gemini API để phân tích đề toán.
    
    Features:
    - JSON mode for reliable output
    - Retry logic with different prompts
    - Smart chunking for long documents
    - Parallel processing with rate limiting
    - Robust JSON extraction
    - LaTeX output format
    """
    
    # ========== SYSTEM PROMPT ==========
    SYSTEM_PROMPT = """
You are an expert in Mathematics, OCR reasoning, and Educational Data Processing for the SmartEdu System with utmost meticulousness.

Your task: Analyze the provided mathematical document (PDF/image/text) along with the JSON snippet containing the problem, the problem's starting page, and the answer's starting page. Carefully read and understand the file and the JSON extract ALL problems into a clean, fully structured JSON format, with all mathematical expressions converted to LaTeX. Read the document carefully because the answer will be located below or elsewhere. Before answering, list all locations in the document that contain information relevant to the question. Analyze the logic between the sections (e.g., question on page 1, answer on page 40). Don't jump to conclusions until you've reviewed all pages. This is a crucial task; a mistake in missing the answer (if the document actually contains it) will corrupt the entire data system. If you are unsure, use a search tool or reread the document. NEVER say "there is no answer" without checking the appendix at the end of the page. After extracting the information, ask yourself: "Did I miss any pages at the end of the document?" and "Does this answer actually belong to this question?"
Extract mathematical content from an image to JSON. Do not solve it manually. Preserve the coefficients. Use LaTeX $...$.

MANDATORY RULES:

1. General Principles
- Return ONLY a single JSON array — no markdown, no explanation, no additional text.
- Each math problem → 1 independent JSON object
- DO NOT generate ID (prevents DB duplication)
- DO NOT modify mathematical content even if errors are detected - COPY VERBATIM
- DO NOT swap coefficients (e.g., "3x + y" must stay "3x + y", NOT "x + 3y")
- DO NOT optimize, simplify, or add reasoning
- Do not skip any questions.
- The question numbers may be incorrect/jumped/repeated → analyze based on content.
- Match the correct answer with the corresponding question (answers may be at the end of the file or interspersed).
- STRICTLY follow the original answer's language, order, and logic

2. LaTeX Formatting (CRITICAL)
- ALL mathematical expressions MUST use LaTeX syntax
- Fractions: `\\frac{numerator}{denominator}`
- Square root: `\\sqrt{x}`, nth root: `\\sqrt[n]{x}`
- Powers: `x^{2}`, `x^{n}`
- Subscripts: `x_{1}`, `a_{n}`
- Roots: \\sqrt{x}, \\sqrt[n]{x}
- Greek letters: `\\alpha`, `\\beta`, `\\pi`
- Inequalities: `\\ne`, `\\le`, `\\ge`, `>`, `<`
- Special Symbols: \\pi, \\infty, \\pm, \\cdot, \\le, \\ge, \\ne
- Double Backslash: Every backslash \\ in LaTeX must be escaped as \\\\ within the JSON string.
- Use standard LaTeX syntax: \\frac{a}{b}, \\sqrt{x}, \\ge, \\le, \\ne, \\dots.
- In JSON strings, backslashes must be escaped: \\ becomes \\\\. For example: $\\frac{1}{2}$ becomes $\\\\frac{1}{2}$.
- Inline Math: Wrap all mathematical expressions with single dollar signs $ ... $.
- Consistency: Ensure all variables and formulas are consistently formatted throughout the JSON.

3. Multi-part Questions (a, b, c...)
- Keep as ONE object (do not split)
- In `solution_steps`, MUST include separators:
  - `--- a)`
  - `--- b)`
  - `--- c)`
4. Questions Without Answers
- If the document does not provide a solution and answer:
    "answer": ""
    "solution_steps": []
- Never invent missing solutions
- DO NOT create your own solution
- If there is no answer that is null

5. Images / Graphs / Tables
- DO NOT describe images if not described in original
- Use ONLY these standard placeholders:
| Case | Placeholder |
|------|-------------|
| Geometric figure | `[HÌNH VẼ]` |
| Graph/Chart | `[ĐỒ THỊ]` |
| Data table | `[BẢNG DỮ LIỆU]` |
| Illustration | `[HÌNH MINH HỌA]` |

ANALYSIS PIPELINE:
Phase 1: Document Mapping: Before extraction, create a distribution diagram: Which page is the question located on? Is there an answer key at the end? Are detailed solutions interspersed after each group of questions?
Phase 2: Reverse Verification: After extracting a question without finding the answer, the AI must perform a specific "Search" command within the file using the question number as the keyword (e.g., "Question 2", "Question 2", "2") on ALL remaining pages before concluding that there is no answer.

Step 1: Read & Identify
- Count expressions/equations
- Identify mathematical tasks
- Convert all math to LaTeX

Step 2: Split Problems
| Scenario | Action |
|----------|--------|
| 1 expression – multiple tasks | 1 object with parts a/b/c |
| Multiple independent expressions | Split into multiple objects |
| Sub-question depends on previous result | Keep in same object |

Step 3: Match Answers
- Check if numbering aligns
- Detect merged/missing answers
- DO NOT edit mathematical content

JSON SCHEMA:
{
  "question": "<string: full question with LaTeX math notation>",
  "type": "<string: TL|TN|Rút gọn biểu thức|So sánh|Chứng minh|Tính toán|Nhận xét đồ thị>",
  "topic": "<string: curriculum topic>",
  "difficulty": "<string: NB|TH|VD|VDC>",
  "grade": <integer: 6-12>,
  "chapter": "<string: chapter name from curriculum>",
  "lesson_title": "<string: lesson title from curriculum>",
  "solution_steps": ["<array of strings: step-by-step solution with LaTeX>"],
  "answer": "<string: final answer with LaTeX if needed>"
}

CURRICULUM CLASSIFICATION (GDPT 2018 - Kết nối tri thức):
You MUST classify each question into the correct grade (6-12), chapter, and lesson title based on the Vietnamese math curriculum below.

TOÁN 6: C1.Số tự nhiên|C2.Tính chia hết|C3.Số nguyên|C4.Hình phẳng và đối xứng|C5.Phân số|C6.Số thập phân|C7.Hình học: điểm,đường thẳng,góc
TOÁN 7: C1.Số hữu tỉ|C2.Số thực|C3.Góc và đường thẳng song song|C4.Tam giác bằng nhau|C5.Thu thập dữ liệu|C6.Tỉ lệ thức|C7.Biểu thức đại số|C8.Đa giác|C9.Biến cố và xác suất
TOÁN 8: C1.Đa thức|C2.Hằng đẳng thức|C3.Tứ giác|C4.Định lí Thalès|C5.Dữ liệu và biểu đồ|C6.Phân thức đại số|C7.PT bậc nhất và hàm số|C8.Xác suất|C9.Tam giác đồng dạng|C10.Hình chóp
TOÁN 9: C1.Hệ PT bậc nhất|C2.Bất PT bậc nhất|C3.Căn thức|C4.Hệ thức lượng tam giác vuông|C5.Đường tròn|C6.Hàm số y=ax²|C7.Tần số và tần số tương đối|C8.Mô hình xác suất|C9.Đường tròn ngoại tiếp,nội tiếp|C10.Hình trụ,hình nón,hình cầu
TOÁN 10: C1.Mệnh đề và tập hợp|C2.BPT bậc nhất hai ẩn|C3.Hệ thức lượng trong tam giác|C4.Vectơ|C5.Các số đặc trưng mẫu số liệu|C6.Hàm số bậc hai|C7.Tọa độ trong mặt phẳng|C8.Đại số tổ hợp|C9.Xác suất cổ điển
TOÁN 11: C1.Hàm số lượng giác và PT lượng giác|C2.Dãy số, cấp số cộng, cấp số nhân|C3.Mẫu số liệu ghép nhóm|C4.Quan hệ song song trong không gian|C5.Giới hạn và hàm số liên tục|C6.Hàm số mũ và logarit|C7.Quan hệ vuông góc trong không gian|C8.Quy tắc tính xác suất|C9.Đạo hàm
TOÁN 12: C1.Ứng dụng đạo hàm để khảo sát và vẽ đồ thị hàm số|C2.Vectơ trong không gian|C3.Các số đặc trưng đo mức độ phân tán|C4.Nguyên hàm và tích phân|C5.Phương pháp tọa độ trong không gian|C6.Xác suất có điều kiện

CLASSIFICATION RULES:
- Analyze the mathematical content of each question to determine which grade level and chapter it belongs to
- Match to the MOST SPECIFIC lesson/topic within the chapter
- If a question spans multiple topics, classify by the PRIMARY skill being tested
- For "grade", return an integer (6, 7, 8, 9, 10, 11, or 12)
- For "chapter", return the full chapter name (e.g. "Chương I. Ứng dụng đạo hàm để khảo sát và vẽ đồ thị hàm số")
- For "lesson_title", return the specific lesson/topic name (e.g. "Tính đơn điệu và cực trị của hàm số")

DIFFICULTY LEVELS:
| Code | Meaning |
|------|---------|
| NB | Nhận biết (Recognition) |
| TH | Thông hiểu (Comprehension) |
| VD | Vận dụng (Application) |
| VDC | Vận dụng cao (Advanced Application) |

OUTPUT FORMAT:
Return PURE JSON ONLY - no markdown, no code blocks, no explanations.
Start with `[` and end with `]`
[
  {
    "question": "...",
    "type": "...",
    "topic": "...",
    "difficulty": "...",
    "grade": 12,
    "chapter": "...",
    "lesson_title": "...",
    "solution_steps": [],
    "answer": "..."
  }
]

STRICT JSON RULES:
- Quoting: All keys and string values MUST use double quotes ("). Never use single quotes (').
- Backslash Escaping: Every LaTeX backslash \\ must be escaped as \\\\ to be valid within a JSON string (e.g., \\\\frac, \\\\sqrt).
- Internal Quotes: Escape any double quotes inside a string as \\".
- No Trailing Commas: Ensure there are no commas after the last element in an object or array.
- Empty Values: Use [] for empty arrays and "" for empty strings.
- No Markdown Wrappers: Output the raw JSON string only. Do NOT include ```json blocks or any conversational text.

CRITICAL REQUIREMENTS:
- Process 100% of problems in file
- DO NOT stop midway even if file is long
- Output ONLY valid JSON array
- NO text before or after JSON
- ALL math expressions in LaTeX format
- Detect and extract all problems even across many pages
- Merge OCR-broken lines
- Always return one single JSON array
- If the file is long, you must complete it; do not stop midway.

RULES FOR CAREFUL RESEARCH:
- Double scan: First, scan the question; second, scan the entire file (especially the last pages) to find the answer key or detailed solution.
- Cross-check: When you see a question, do not leave the answer field blank unless you have searched every page and still can't find it. Note that the answer may not be directly under the question but clustered in a separate section.
- Contextual analysis: If the question is 'Exercise 2' but the answer is 'Question 2', use mathematical logic to check if they match in content.
- Begin your analysis slowly and carefully.

CRITICAL WARNINGS - VIOLATION = COMPLETE FAILURE

YOU MUST COPY MATHEMATICAL EXPRESSIONS EXACTLY. DO NOT:
- Change √x + 4 to √(x+4) - THESE ARE DIFFERENT!
- Change √x - 1 to √(x-1) - THESE ARE DIFFERENT!
- Change 3√x + 1 to 3√(x+1) - THESE ARE DIFFERENT!
- Move numbers inside/outside radicals
- Swap coefficients (3x+y ≠ x+3y)
- Reorder terms
- "Fix" or "correct" anything

CORRECT EXAMPLES:
- "√x + 4" stays as "$\\sqrt{x} + 4$" (4 is OUTSIDE the radical)
- "√x - 1" stays as "$\\sqrt{x} - 1$" (1 is OUTSIDE the radical)
- "3√x + 1" stays as "$3\\sqrt{x} + 1$" (NOT $3\\sqrt{x+1}$)
- "x + 2√x - 3" stays as "$x + 2\\sqrt{x} - 3$"

WRONG EXAMPLES (NEVER DO THIS):
- "√x + 4" → "$\\sqrt{x+4}$" (WRONG - moved 4 inside)
- "√x - 1" → "$\\sqrt{x-1}$" (WRONG - moved 1 inside)

BEGIN ANALYSIS NOW
"""

    # Primary prompt - EXTREMELY STRICT about not modifying content
    PARSE_PROMPT_V1 = """
TASK: Extract verbatim all math questions from the following text into a JSON array.
CRITICAL OUTPUT RULE:
- You MUST close all JSON strings, arrays, and objects.
- If output is long, you MUST finish the current JSON object before stopping.
- NEVER cut output in the middle of a string.
- If you are running out of tokens, STOP AFTER closing the JSON array.
- Do not make up answers if you don't have them.
If solution steps are very long:
- Keep solution_steps concise
- Prefer formulas over text
- Do NOT repeat explanations

ABSOLUTE RULE – VIOLATION = FAIL:
1. COPY VERBATIM mathematical content - DO NOT change ANY numbers, variables, coefficients
2. If the title says "x + y = 3", the output MUST be "x + y = 3", NOT change to "x + 2y = 3"
3. If the title says "3x + y = 1", the output MUST be "3x + y = 1", NOT change to "x + 3y = 1"
4. NO error correction, NO optimization, NO change of order
5. Only switch to LaTeX syntax, keep the same value

WRONG EXAMPLES (ABSOLUTELY NOT):
- "√x + 4" → "$sqrt{{x+4}}$" (WRONG - put 4 in the unit)
- "√x - 1" → "$sqrt{{x-1}}$" (WRONG - put 1 in the unit)
- "3√x + 1" → "$3sqrt{{x+1}}$" (FALSE)

TRUE EXAMPLE:
- "√x + 4" → "$sqrt{{x}} + 4$" (4 in OUTSIDE)
- "√x - 1" → "$sqrt{{x}} - 1$" (1 in OUTSIDE)
- "A = (√x + 4)/(√x - 1)" → "$A = frac{{sqrt{{x}} + 4}}{{sqrt{{x}} - 1}}$"

MATH PROBLEMS:
{text}

OUTPUT: JSON array, starts with [ ends with ], no other text:"""

    # Backup prompt - even more explicit
    PARSE_PROMPT_V2 = """
Extract math questions into JSON.
CRITICAL OUTPUT RULE:
- You MUST close all JSON strings, arrays, and objects.
- If output is long, you MUST finish the current JSON object before stopping.
- NEVER cut output in the middle of a string.
- If you are running out of tokens, STOP AFTER closing the JSON array.

If solution steps are very long:
- Keep solution_steps concise
- Prefer formulas over text
- Do NOT repeat explanations

IMPORTANT: Copy each number, each variable, each coefficient exactly. NO has changed.
- "x + y = 3" → keep "x + y = 3"
- "2x² + y²" → keep "$2x^2 + y^2$"
- NO swap multiplier, NO modification

WRONG EXAMPLES (ABSOLUTELY NOT):
- "√x + 4" → "$sqrt{{x+4}}$" (WRONG - put 4 in the unit)
- "√x - 1" → "$sqrt{{x-1}}$" (WRONG - put 1 in the unit)
- "3√x + 1" → "$3sqrt{{x+1}}$" (FALSE)

TRUE EXAMPLE:
- "√x + 4" → "$sqrt{{x}} + 4$" (4 in OUTSIDE)
- "√x - 1" → "$sqrt{{x}} - 1$" (1 in OUTSIDE)
- "A = (√x + 4)/(√x - 1)" → "$A = frac{{sqrt{{x}} + 4}}{{sqrt{{x}} - 1}}$"

{text}

JSON array (bắt đầu với [):"""

    # Last resort prompt - minimal but strict
    PARSE_PROMPT_V3 = """
Extract math questions to JSON. COPY ALL NUMBERS AND COEFFICIENTS EXACTLY AS WRITTEN.

{text}
CRITICAL OUTPUT RULE:
- You MUST close all JSON strings, arrays, and objects.
- If output is long, you MUST finish the current JSON object before stopping.
- NEVER cut output in the middle of a string.
- If you are running out of tokens, STOP AFTER closing the JSON array.

If solution steps are very long:
- Keep solution_steps concise
- Prefer formulas over text
- Do NOT repeat explanations

REMEMBER: Do NOT change "x + y = 3" to "x + 2y = 3". Copy EXACTLY.
CRITICAL: 
- "√x + 4" means √x PLUS 4, NOT √(x+4)
- "√x - 1" means √x MINUS 1, NOT √(x-1)
- Keep numbers in their EXACT positions
JSON array:"""

    # Vision prompt for image-based extraction
    VISION_PROMPT = """
🎯 CRITICAL TASK: Extract 100% of ALL math questions visible in these page images.

⛔ STRICT RULES:
1. You MUST extract EVERY SINGLE question visible in ALL pages - missing even ONE question is UNACCEPTABLE
2. ONLY extract questions that are VISIBLE in the images - DO NOT invent or hallucinate
3. If you cannot read the image clearly, return an empty array []
4. Scan EACH PAGE carefully from top to bottom, left to right
5. Count the questions as you go: "Page 1: questions 1-3", "Page 2: questions 4-6", etc.

📋 EXTRACTION CHECKLIST:
- [ ] Have I checked ALL pages provided?
- [ ] Have I extracted EVERY question from EACH page?
- [ ] Have I included questions that span multiple parts (a, b, c)?
- [ ] Have I checked for questions in margins, footers, or side columns?

📝 FORMAT RULES:
1. Convert all mathematical expressions to proper LaTeX format
2. COPY formulas EXACTLY as shown - do not modify any numbers or coefficients
3. Match answers with their corresponding questions if visible
4. Include solution steps if shown in the document
5. If a question has NO answer/solution visible, set answer="" and solution_steps=[]
6. Keep multi-part questions (a, b, c) as ONE object

For fractions like:
  x + 1
  -----  should become $\\frac{x+1}{x-1}$
  x - 1

For square roots: √x + 4 should become $\\sqrt{x} + 4$ (4 is OUTSIDE)

OUTPUT FORMAT:
Return ONLY a valid JSON array, no markdown, no explanation:
[
  {
    "question": "Full question text with LaTeX math",
    "type": "TL|TN|Rút gọn biểu thức|So sánh|Chứng minh|Tìm GTNN|Tìm x|Giải phương trình",
    "topic": "Topic name",
    "difficulty": "NB|TH|VD|VDC",
    "grade": 12,
    "chapter": "Chapter name from curriculum",
    "lesson_title": "Lesson title from curriculum",
    "solution_steps": ["step 1", "step 2"],
    "answer": "Final answer"
  }
]

🚨 FINAL CHECK: Before submitting, count your extracted questions and verify you didn't miss any!
Now extract ALL visible questions:"""

    def __init__(
        self,
        provider: AIProvider = AIProvider.GEMINI,
        gemini_api_key: Optional[str] = None,
        gemini_model: str = None,
        max_tokens: int = 65536,  # Safe limit for output
        max_chunk_size: int = 20000,  # Balanced chunk size
        max_concurrency: int = 3  # Parallel requests
    ):
        self.provider = provider
        self.gemini_api_key = gemini_api_key or os.getenv("GOOGLE_API_KEY")
        self.gemini_model = gemini_model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self.max_tokens = max_tokens
        self.max_chunk_size = max_chunk_size
        self.max_concurrency = max_concurrency
        
        # Semaphore for rate limiting
        self._semaphore = asyncio.Semaphore(max_concurrency)
        
        # Store for cross-chunk answer matching
        self._answer_pool: Dict[str, str] = {}
        
        self._client = None
        self._init_clients()
    
    def _init_clients(self):
        """Initialize Gemini client using google-genai SDK"""
        self._client = None
        
        if not self.gemini_api_key:
            logger.warning("No GOOGLE_API_KEY found")
            return

        try:
            from google import genai

            self._client = genai.Client(api_key=self.gemini_api_key)

            logger.info(f"Gemini initialized: model={self.gemini_model}, concurrency={self.max_concurrency}, chunk={self.max_chunk_size}")

        except ImportError:
            logger.error("google-genai not installed. Run: pip install google-genai")
        except Exception as e:
            logger.error(f"Gemini init error: {e}")

    
    def _get_available_provider(self) -> Optional[AIProvider]:
        """Get available provider"""
        if self._client:
            return AIProvider.GEMINI
        return None
    
    async def parse(self, text: str, progress_callback: Optional[Callable[[int, int], None]] = None) -> List[Dict[str, Any]]:
        """Main entry point - parse text into questions"""
        if not text or not text.strip():
            return []

        # Fail fast if no AI provider
        if not self._client:
            raise RuntimeError(
                "GOOGLE_API_KEY chưa được cấu hình. "
                "Vui lòng thêm API key trong Settings → Environment Variables."
            )
        
        text = self._clean_text(text)
        start_time = time.time()
        logger.info(f"Document length: {len(text):,} chars")
        
        # Reset answer pool for new parse
        self._answer_pool = {}
        
        # Always chunk for consistency (even small docs benefit from structured processing)
        if len(text) > self.max_chunk_size:
            result = await self._parse_chunked_parallel(text, progress_callback)
        else:
            result = await self._parse_single(text, chunk_id=0)
        
        elapsed = time.time() - start_time
        logger.info(f"Total time: {elapsed:.1f}s ({len(result)} questions)")
        return result
    
    async def parse_images(self, images: List[Dict], progress_callback: Optional[Callable[[int, int], None]] = None) -> List[Dict[str, Any]]:
        """
        Parse questions from images using Vision API.
        """
        if not images:
            return []

        # Fail fast if no AI provider
        if not self._client:
            raise RuntimeError(
                "GOOGLE_API_KEY chưa được cấu hình. "
                "Vui lòng thêm API key trong Settings → Environment Variables."
            )
        
        start_time = time.time()
        total_pages = len(images)
        logger.info(f"Processing {total_pages} page images with Vision API")
        
        # Reset answer pool
        self._answer_pool = {}
        
        # Process images in batches
        # For small PDFs (<=15 pages), send all at once for better context
        # For larger PDFs, use batch_size of 10 for better extraction
        if total_pages <= 15:
            batch_size = total_pages  # Send all at once
            logger.info(f"Small PDF detected - sending all {total_pages} pages at once for better accuracy")
        else:
            batch_size = 10  # Increased from 3 to 10 for better context
            logger.info(f"Large PDF detected - processing in batches of {batch_size} pages")
        
        all_questions = []
        seen_hashes = set()
        
        for batch_start in range(0, total_pages, batch_size):
            batch_end = min(batch_start + batch_size, total_pages)
            batch_images = images[batch_start:batch_end]
            
            logger.info(f"Processing pages {batch_start + 1}-{batch_end}/{total_pages}...")
            
            async with self._semaphore:
                result = await self._call_gemini_vision(batch_images)
                
                for q in result:
                    q_hash = self._hash_question(q.get("question", ""))
                    if q_hash and q_hash not in seen_hashes:
                        seen_hashes.add(q_hash)
                        all_questions.append(q)
                        self._collect_answers([q])
            
            if progress_callback:
                progress_callback(batch_end, total_pages)
        
        # Cross-page answer matching
        all_questions = self._match_answers_from_pool(all_questions)
        
        elapsed = time.time() - start_time
        logger.info(f"Vision processing total: {elapsed:.1f}s ({len(all_questions)} questions)")
        return all_questions
    
    async def _call_gemini_vision(self, images: List[Dict]) -> List[Dict[str, Any]]:
        """Call Gemini Vision API with native async + structured output.

        Sprint 2: native async (Task 9) + schema mode (Task 10).
        """
        if not self._client:
            logger.warning("No Gemini client available")
            return []

        from google.genai import types

        # Build content parts — text prompt + images
        parts = [self.VISION_PROMPT]
        for img in images:
            parts.append(types.Part.from_bytes(
                data=base64.b64decode(img["data"]),
                mime_type=img.get("mime_type", "image/jpeg"),
            ))

        content = ""

        # ── Tier 1: Schema mode ──
        try:
            response = await self._client.aio.models.generate_content(
                model=self.gemini_model,
                contents=parts,
                config=types.GenerateContentConfig(
                    system_instruction=self.SYSTEM_PROMPT,
                    temperature=0,
                    max_output_tokens=self.max_tokens,
                    response_mime_type="application/json",
                    response_schema=PARSE_SCHEMA,
                ),
            )
            content = self._safe_text(response)
            if content:
                result = self._extract_json(content)
                if result:
                    logger.info(f"Vision schema mode: {len(result)} questions from {len(images)} pages")
                    return result
        except Exception as e:
            logger.warning(f"Vision schema mode failed: {e}")

        # ── Tier 2: JSON mode ──
        try:
            response = await self._client.aio.models.generate_content(
                model=self.gemini_model,
                contents=parts,
                config=types.GenerateContentConfig(
                    system_instruction=self.SYSTEM_PROMPT,
                    temperature=0,
                    max_output_tokens=self.max_tokens,
                    response_mime_type="application/json",
                ),
            )
            content = self._safe_text(response)
            if content:
                result = self._extract_json(content)
                if result:
                    logger.info(f"Vision JSON mode: {len(result)} questions from {len(images)} pages")
                    return result
        except Exception as e:
            logger.warning(f"Vision JSON mode failed: {e}")

        # ── Tier 3: Plain text ──
        try:
            response = await self._client.aio.models.generate_content(
                model=self.gemini_model,
                contents=parts,
                config=types.GenerateContentConfig(
                    system_instruction=self.SYSTEM_PROMPT,
                    temperature=0,
                    max_output_tokens=self.max_tokens,
                ),
            )
            content = self._safe_text(response)
            if content:
                result = self._extract_json(content)
                logger.info(f"Vision plain text: {len(result)} questions from {len(images)} pages")
                return result
        except Exception as e:
            logger.error(f"Vision all tiers failed: {e}")

        if not content:
            return []

        result = self._extract_json(content)
        logger.info(f"Vision extracted {len(result)} questions from {len(images)} pages")
        if len(result) == 0:
            logger.warning(f"No questions extracted! Response preview: {content[:500]}...")
        elif len(result) < len(images) * 0.5:
            logger.warning(f"Low extraction rate ({len(result)} questions from {len(images)} pages). May need retry.")
        return result
    
    async def _parse_single(self, text: str, chunk_id: int = 0) -> List[Dict[str, Any]]:
        """Parse single chunk with retry logic and rate limiting"""
        async with self._semaphore:
            provider = self._get_available_provider()
            
            if not provider:
                raise RuntimeError("GOOGLE_API_KEY chưa được cấu hình.")
            
            prompts = [
                self.PARSE_PROMPT_V1,
                self.PARSE_PROMPT_V2,
                self.PARSE_PROMPT_V3
            ]
            
            last_error = None
            last_content = ""
            
            for attempt, prompt_template in enumerate(prompts):
                try:
                    logger.info(f"Chunk {chunk_id} - Attempt {attempt + 1}/{len(prompts)}...")
                    result, raw_content = await self._call_gemini(text, prompt_template)
                    last_content = raw_content
                    
                    if result and len(result) > 0:
                        logger.info(f"Chunk {chunk_id} - Extracted {len(result)} questions")
                        # Collect answers for cross-chunk matching
                        self._collect_answers(result)
                        return result
                    else:
                        logger.warning(f"Chunk {chunk_id} - Attempt {attempt + 1}: Empty result")
                        
                except Exception as e:
                    last_error = e
                    logger.error(f"Chunk {chunk_id} - Attempt {attempt + 1} failed: {e}")
                    await asyncio.sleep(0.5)
            
            # Try to salvage from last response
            logger.warning(f"Chunk {chunk_id} - Trying aggressive JSON extraction...")
            if last_content:
                result = self._aggressive_extract_json(last_content)
                if result:
                    logger.info(f"Chunk {chunk_id} - Salvaged {len(result)} questions")
                    self._collect_answers(result)
                    return result
            
            logger.error(f"Chunk {chunk_id} - All AI attempts failed, returning empty")
            return []
    
    async def _call_gemini(self, text: str, prompt_template: str) -> tuple[List[Dict], str]:
        """Call Gemini API with native async + structured output.

        3-tier fallback with retry on 429 rate limit.
        """
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
                        logger.warning(f"{label} rate limited (attempt {attempt+1}/3), waiting {wait}s...")
                        await asyncio.sleep(wait)
                        continue
                    logger.warning(f"{label} failed: {e}")
                    return None, ""
            return None, ""

        # ── Tier 1: Schema mode ──
        result, content = await _try_with_retry(
            types.GenerateContentConfig(
                system_instruction=self.SYSTEM_PROMPT,
                temperature=0,
                max_output_tokens=self.max_tokens,
                response_mime_type="application/json",
                response_schema=PARSE_SCHEMA,
            ),
            "Schema mode"
        )
        if result:
            return result, content

        # ── Tier 2: JSON mode without schema ──
        result, content = await _try_with_retry(
            types.GenerateContentConfig(
                system_instruction=self.SYSTEM_PROMPT,
                temperature=0,
                max_output_tokens=self.max_tokens,
                response_mime_type="application/json",
            ),
            "JSON mode"
        )
        if result:
            return result, content

        # ── Tier 3: Plain text ──
        result, content = await _try_with_retry(
            types.GenerateContentConfig(
                system_instruction=self.SYSTEM_PROMPT,
                temperature=0,
                max_output_tokens=self.max_tokens,
            ),
            "Plain text mode"
        )
        if result:
            return result, content

        # Return whatever content we got for aggressive extraction
        return [], content

    @staticmethod
    def _safe_text(response) -> str:
        """Safely extract text from Gemini response."""
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
    
    async def _parse_chunked_parallel(self, text: str, progress_callback: Optional[Callable] = None) -> List[Dict[str, Any]]:
        """⚡ Parallel chunk processing with improved merging"""
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
        
        # Run all chunks in parallel (semaphore controls concurrency)
        tasks = [process_chunk(i, chunk) for i, chunk in enumerate(chunks)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Sort by chunk index to maintain order
        sorted_results = []
        for r in results:
            if isinstance(r, Exception):
                logger.error(f"Chunk failed: {r}")
                continue
            sorted_results.append(r)
        
        sorted_results.sort(key=lambda x: x[0])
        
        # Merge results with deduplication
        all_questions = []
        seen_hashes = set()
        
        for idx, questions in sorted_results:
            for q in questions:
                q_hash = self._hash_question(q.get("question", ""))
                if q_hash and q_hash not in seen_hashes:
                    seen_hashes.add(q_hash)
                    all_questions.append(q)
        
        # Cross-chunk answer matching
        all_questions = self._match_answers_from_pool(all_questions)
        
        logger.info(f"Total: {len(all_questions)} unique questions")
        return all_questions
    
    def _collect_answers(self, questions: List[Dict]):
        """Collect answers from parsed questions for cross-chunk matching"""
        for q in questions:
            q_text = q.get("question", "").strip()
            answer = q.get("answer", "").strip()
            
            # Extract question number
            num_match = re.search(r'(?:Câu|Bài|Question)?\s*(\d+)', q_text, re.IGNORECASE)
            if num_match and answer:
                self._answer_pool[num_match.group(1)] = answer
            
            # Also check for standalone answer entries (e.g., "Câu 1: A")
            if len(q_text) < 50:
                ans_match = re.match(r'^(?:Câu|Bài)?\s*(\d+)\s*[:.]\s*([A-D]|.{1,50})$', q_text, re.IGNORECASE)
                if ans_match:
                    self._answer_pool[ans_match.group(1)] = ans_match.group(2)
    
    def _match_answers_from_pool(self, questions: List[Dict]) -> List[Dict]:
        """Match answers from pool to questions without answers"""
        for q in questions:
            if q.get("answer"):
                continue
            
            q_text = q.get("question", "")
            num_match = re.search(r'(?:Câu|Bài|Question)?\s*(\d+)', q_text, re.IGNORECASE)
            
            if num_match and num_match.group(1) in self._answer_pool:
                q["answer"] = self._answer_pool[num_match.group(1)]
        
        # Remove standalone answer entries (they've been merged)
        result = []
        for q in questions:
            q_text = q.get("question", "").strip()
            if len(q_text) < 50:
                if re.match(r'^(?:Câu|Bài)?\s*\d+\s*[:.]\s*[A-D]?\s*$', q_text, re.IGNORECASE):
                    continue
            result.append(q)
        
        return result
    
    def _hash_question(self, text: str) -> str:
        """Create hash for deduplication"""
        if not text:
            return ""
        normalized = re.sub(r'\s+', ' ', text.lower().strip())[:150]
        return normalized
    
    def _smart_chunk(self, text: str) -> List[str]:
        """Smart chunking by question boundaries"""
        question_patterns = [
            r'\n\s*Câu\s+\d+',
            r'\n\s*Bài\s+\d+',
            r'\n\s*\d+\.\s+',
            r'\n\s*\d+\)\s+',
            r'\n\s*[IVX]+\.\s+',
            r'\n\s*Question\s+\d+',
        ]
        
        pattern = '|'.join(f'({p})' for p in question_patterns)
        splits = list(re.finditer(pattern, text, re.IGNORECASE))
        
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
        
        return chunks if chunks else [text]
    
    def _chunk_by_size(self, text: str) -> List[str]:
        """Fallback: chunk by size with smart breaks"""
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
    
    def _clean_text(self, text: str) -> str:
        """Clean and normalize text"""
        text = re.sub(r'\n{4,}', '\n\n\n', text)
        text = re.sub(r' {3,}', '  ', text)
        text = re.sub(r'\t+', ' ', text)
        return text.strip()
    
    def _extract_json(self, content: str) -> List[Dict]:
        """Extract JSON from response with robust error handling"""
        if not content:
            logger.warning("_extract_json: Content is empty")
            return []
        
        content = content.strip()
        
        # Check if truncated
        if not content.rstrip().endswith(']'):
            logger.warning(f"JSON may be truncated. Last 100 chars: ...{content[-100:]}")
        
        # Pre-fix: Fix triple backslashes (common Gemini issue with LaTeX)
        content = re.sub(r'\\\\\\+', r'\\\\', content)
        
        # Method 1: Direct parse
        try:
            result = json.loads(content)
            if isinstance(result, list):
                logger.info(f"Direct parse success: {len(result)} items")
                return result
        except json.JSONDecodeError as e:
            logger.warning(f"Direct parse failed: {e}")
        
        # Method 2: Remove markdown
        if "```json" in content:
            try:
                json_str = content.split("```json")[1].split("```")[0].strip()
                json_str = re.sub(r'\\\\\\+', r'\\\\', json_str)  # Fix triple backslash
                result = json.loads(json_str)
                if isinstance(result, list):
                    logger.info(f"Markdown parse success: {len(result)} items")
                    return result
            except Exception as e:
                logger.warning(f"Markdown parse failed: {e}")
        
        if "```" in content:
            for part in content.split("```"):
                part = part.strip()
                if part.startswith("["):
                    try:
                        part = re.sub(r'\\\\\\+', r'\\\\', part)
                        result = json.loads(part)
                        if isinstance(result, list):
                            return result
                    except:
                        continue
        
        # Method 3: Aggressive extraction with fixes
        return self._aggressive_extract_json(content)
    
    def _aggressive_extract_json(self, content: str) -> List[Dict]:
        """More aggressive JSON extraction with multiple fix attempts"""
        if not content:
            return []
        
        start_idx = content.find('[')
        if start_idx == -1:
            logger.warning("No '[' found in content")
            return []
        
        # Find matching closing bracket
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
            
            if char == '"' and not escape_next:
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
            # Try to find last ] even if brackets don't match
            last_bracket = content.rfind(']')
            if last_bracket > start_idx:
                end_idx = last_bracket + 1
                logger.warning(f"Brackets unmatched, using last ']' at position {last_bracket}")
            else:
                logger.warning("Could not find matching ']'")
                return []
        
        json_str = content[start_idx:end_idx]
        
        # Multiple fix attempts - ORDER MATTERS!
        fix_attempts = [
            # Attempt 1: Fix triple backslashes FIRST (most common Gemini issue)
            ("Fix triple backslash", lambda s: re.sub(r'\\\\\\+', r'\\\\', s)),
            # Attempt 2: Basic fixes (trailing commas)
            ("Fix trailing commas", lambda s: re.sub(r',\s*]', ']', re.sub(r',\s*}', '}', s))),
            # Attempt 3: Fix Python literals
            ("Fix Python literals", lambda s: s.replace('None', 'null').replace('True', 'true').replace('False', 'false')),
            # Attempt 4: Remove control characters
            ("Remove control chars", lambda s: re.sub(r'[\x00-\x1f\x7f-\x9f]', '', s)),
            # Attempt 5: Fix newlines in strings
            ("Fix newlines", lambda s: re.sub(r'(?<!\\)\n', '\\n', s)),
        ]
        
        current = json_str
        
        # Apply ALL fixes first
        for name, fix in fix_attempts:
            try:
                current = fix(current)
            except Exception as e:
                logger.warning(f"Fix '{name}' failed: {e}")
        
        # Try to parse
        try:
            result = json.loads(current)
            if isinstance(result, list):
                logger.info(f"Aggressive parse success after fixes: {len(result)} items")
                return result
        except json.JSONDecodeError as e:
            logger.warning(f"Parse after all fixes failed: {e}")
            logger.debug(f"JSON snippet (first 500 chars): {current[:500]}")
        
        # Last resort: Try to extract individual objects
        logger.warning("Trying to extract individual JSON objects...")
        return self._extract_individual_objects(current)
    
    def _mock_parse(self, text: str) -> List[Dict[str, Any]]:
        """Fallback regex parser"""
        questions = []
        
        patterns = [
            (r'(?:Câu|Bài)\s*(\d+)[.:]\s*(.*?)(?=(?:Câu|Bài)\s*\d+[.:]|ĐÁP ÁN|PHẦN|$)', re.DOTALL | re.IGNORECASE),
            (r'(\d+)\.\s+(.*?)(?=\d+\.\s+|ĐÁP ÁN|$)', re.DOTALL),
        ]
        
        for pattern, flags in patterns:
            matches = re.findall(pattern, text, flags)
            if matches:
                for num, content in matches:
                    content = content.strip()
                    if len(content) > 15:
                        q_type = "TL"
                        if re.search(r'[A-D]\s*[.)]', content):
                            q_type = "TN"
                        
                        answer = ""
                        ans_match = re.search(r'(?:Đáp án|ĐA)[:\s]*([A-D])', content, re.IGNORECASE)
                        if ans_match:
                            answer = ans_match.group(1)
                        
                        questions.append({
                            "question": content,
                            "type": q_type,
                            "topic": "Toán học",
                            "difficulty": "TH",
                            "grade": None,
                            "chapter": "",
                            "lesson_title": "",
                            "solution_steps": [],
                            "answer": answer
                        })
                
                if questions:
                    break
        
        return questions
    
    def _extract_individual_objects(self, json_str: str) -> List[Dict]:
        """Last resort: extract individual JSON objects one by one"""
        objects = []
        
        # Fix triple backslashes first
        json_str = re.sub(r'\\\\\\+', r'\\\\', json_str)
        
        # Find all potential object starts
        obj_starts = [m.start() for m in re.finditer(r'\{\s*"question"', json_str)]
        
        for i, start in enumerate(obj_starts):
            # Find the end of this object (next object start or end of string)
            if i + 1 < len(obj_starts):
                end_search = obj_starts[i + 1]
            else:
                end_search = len(json_str)
            
            # Find closing brace
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
                if char == '"' and not escape_next:
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
            
            obj_str = substring[:obj_end]
            
            # Try to parse this individual object
            try:
                # Apply fixes
                obj_str = re.sub(r',\s*}', '}', obj_str)
                obj_str = re.sub(r'[\x00-\x1f]', '', obj_str)
                obj_str = re.sub(r'\\\\\\+', r'\\\\', obj_str)  # Fix triple backslash again
                
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
                # Try one more fix: escape unescaped backslashes before known LaTeX commands
                try:
                    # This is risky but sometimes works
                    fixed = re.sub(r'\\([a-zA-Z])', r'\\\\\\1', obj_str)
                    fixed = re.sub(r'\\\\\\\\', r'\\\\', fixed)  # Don't over-escape
                    obj = json.loads(fixed)
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
                except:
                    pass
        
        if objects:
            logger.info(f"Extracted {len(objects)} individual objects")
        else:
            logger.warning("Could not extract any individual objects")
        
        return objects


# ============ SPEED PRESETS ============

def create_fast_parser(**kwargs):
    """🚀 Fast: Larger chunks, more parallel"""
    return AIQuestionParser(
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        max_chunk_size=20000,
        max_concurrency=5,
        max_tokens=65536,
        **kwargs
    )

def create_balanced_parser(**kwargs):
    """⚖️ Balanced: Medium settings"""
    return AIQuestionParser(
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        max_chunk_size=15000,
        max_concurrency=3,
        max_tokens=65536,
        **kwargs
    )

def create_quality_parser(**kwargs):
    """🎯 Quality: Smaller chunks, more accurate"""
    return AIQuestionParser(
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        max_chunk_size=10000,
        max_concurrency=2,
        max_tokens=65536,
        **kwargs
    )


# ==================== TEST ====================

if __name__ == "__main__":
    async def test():
        parser = AIQuestionParser()
        
        sample = """
        Câu 1: Giải phương trình x² - 5x + 6 = 0
        A. x = 2, x = 3
        B. x = -2, x = -3
        C. x = 2, x = -3
        D. x = -2, x = 3
        
        Câu 2: Tính đạo hàm của hàm số y = x³ - 3x² + 2
        
        Câu 3: Cho hình chóp S.ABCD có đáy là hình vuông cạnh a. Tính thể tích.
        
        ĐÁP ÁN:
        Câu 1: A
        Câu 2: y' = 3x² - 6x
        """
        
        result = await parser.parse(sample)
        logger.debug(json.dumps(result, indent=2, ensure_ascii=False))
    
    asyncio.run(test())