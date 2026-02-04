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


class AIProvider(Enum):
    CLAUDE = "claude"
    GEMINI = "gemini"
    AUTO = "auto"


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
- If the document provides no solution:
    "answer": ""
    "solution_steps": []
- Never invent missing solutions
- DO NOT create your own solution

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
  "solution_steps": ["<array of strings: step-by-step solution with LaTeX>"],
  "answer": "<string: final answer with LaTeX if needed>"
}

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
Analyze these math exam page images and extract ALL questions into a JSON array.

RULES:
1. Extract EVERY question you can see in the images
2. Convert all mathematical expressions to proper LaTeX format
3. COPY formulas EXACTLY as shown - do not modify any numbers or coefficients
4. Match answers with their corresponding questions if visible
5. Include solution steps if shown

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
    "type": "TL|TN|Rút gọn biểu thức|So sánh|Chứng minh",
    "topic": "Topic name",
    "difficulty": "NB|TH|VD|VDC",
    "solution_steps": ["step 1", "step 2"],
    "answer": "Final answer"
  }
]

Extract all visible questions now:"""

    def __init__(
        self,
        provider: AIProvider = AIProvider.AUTO,
        gemini_api_key: Optional[str] = None,
        gemini_model: str = "gemini-2.5-pro",  # Updated to 2.5
        max_tokens: int = 65536,  # Safe limit for output
        max_chunk_size: int = 20000,  # Balanced chunk size
        max_concurrency: int = 3  # Parallel requests
    ):
        self.provider = provider
        self.gemini_api_key = gemini_api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
        self.gemini_model = gemini_model
        self.max_tokens = max_tokens
        self.max_chunk_size = max_chunk_size
        self.max_concurrency = max_concurrency
        
        # Semaphore for rate limiting
        self._semaphore = asyncio.Semaphore(max_concurrency)
        
        # Store for cross-chunk answer matching
        self._answer_pool: Dict[str, str] = {}
        
        self._gemini_model = None
        self._init_clients()
    
    def _init_clients(self):
        if not self.gemini_api_key:
            print("⚠️ No GOOGLE_API_KEY found")
            return

        try:
            from google import genai
            from google.genai import types


            self._client = genai.Client(api_key=self.gemini_api_key)

            print(f"✅ Gemini (google-genai) initialized")
            print(f"🤖 Model: {self.gemini_model}")
            print(f"⚡ Concurrency: {self.max_concurrency}")

        except ImportError:
            print("⚠️ google-genai not installed. Run: pip install google-genai")
        except Exception as e:
            print(f"⚠️ Gemini init error: {e}")

    
    def _get_available_provider(self) -> Optional[AIProvider]:
        """Get available provider"""
        if self._gemini_model:
            return AIProvider.GEMINI
        return None
    
    async def parse(self, text: str, progress_callback: Optional[Callable[[int, int], None]] = None) -> List[Dict[str, Any]]:
        """Main entry point - parse text into questions"""
        if not text or not text.strip():
            return []
        
        text = self._clean_text(text)
        start_time = time.time()
        print(f"📄 Document length: {len(text):,} chars")
        
        # Reset answer pool for new parse
        self._answer_pool = {}
        
        # Always chunk for consistency (even small docs benefit from structured processing)
        if len(text) > self.max_chunk_size:
            result = await self._parse_chunked_parallel(text, progress_callback)
        else:
            result = await self._parse_single(text, chunk_id=0)
        
        elapsed = time.time() - start_time
        print(f"⏱️ Total time: {elapsed:.1f}s ({len(result)} questions)")
        return result
    
    async def parse_images(self, images: List[Dict], progress_callback: Optional[Callable[[int, int], None]] = None) -> List[Dict[str, Any]]:
        """
        Parse questions from images using Vision API.
        
        Args:
            images: List of {"page": int, "data": base64_string, "mime_type": str}
            progress_callback: Optional callback for progress updates
        
        Returns:
            List of parsed questions
        """
        if not images:
            return []
        
        start_time = time.time()
        total_pages = len(images)
        print(f"📄 Processing {total_pages} page images with Vision API")
        
        # Reset answer pool
        self._answer_pool = {}
        
        # Process images in batches (to avoid rate limits)
        batch_size = 3  # Process 3 pages at a time
        all_questions = []
        seen_hashes = set()
        
        for batch_start in range(0, total_pages, batch_size):
            batch_end = min(batch_start + batch_size, total_pages)
            batch_images = images[batch_start:batch_end]
            
            print(f"🖼️ Processing pages {batch_start + 1}-{batch_end}/{total_pages}...")
            
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
        print(f"⏱️ Vision processing total: {elapsed:.1f}s ({len(all_questions)} questions)")
        return all_questions
    
    async def _call_gemini_vision(self, images: List[Dict]) -> List[Dict[str, Any]]:
        """Call Gemini Vision API with images"""
        if not self._gemini_model:
            print("⚠️ No Gemini model available")
            return []
        
        loop = asyncio.get_event_loop()
        
        def call_api():
            try:
                from google.genai import types
                
                # Build content parts with images
                parts = []
                
                # Add instruction
                parts.append(types.Part.from_text(self.VISION_PROMPT))
                
                # Add images
                for img in images:
                    parts.append(types.Part.from_bytes(
                        data=base64.b64decode(img["data"]),
                        mime_type=img.get("mime_type", "image/jpeg")
                    ))
                
                # Call API
                response = self._gemini_model.client.models.generate_content(
                    model=self._gemini_model.model_id,
                    contents=[types.Content(role="user", parts=parts)],
                    config=types.GenerateContentConfig(
                        system_instruction=self.SYSTEM_PROMPT,
                        temperature=0,
                        max_output_tokens=self.max_tokens,
                        response_mime_type="application/json",
                    )
                )
                
                return response.text
                
            except Exception as e:
                print(f"❌ Vision API error: {e}")
                # Retry without JSON mode
                try:
                    response = self._gemini_model.client.models.generate_content(
                        model=self._gemini_model.model_id,
                        contents=[types.Content(role="user", parts=parts)],
                        config=types.GenerateContentConfig(
                            system_instruction=self.SYSTEM_PROMPT,
                            temperature=0,
                            max_output_tokens=self.max_tokens,
                        )
                    )
                    return response.text
                except Exception as e2:
                    print(f"❌ Vision API retry failed: {e2}")
                    return ""
        
        content = await loop.run_in_executor(None, call_api)
        
        if not content:
            return []
        
        result = self._extract_json(content)
        print(f"✅ Vision extracted {len(result)} questions")
        return result
    
    async def _parse_single(self, text: str, chunk_id: int = 0) -> List[Dict[str, Any]]:
        """Parse single chunk with retry logic and rate limiting"""
        async with self._semaphore:
            provider = self._get_available_provider()
            
            if not provider:
                print("⚠️ No AI provider, using mock parser")
                return self._mock_parse(text)
            
            prompts = [
                self.PARSE_PROMPT_V1,
                self.PARSE_PROMPT_V2,
                self.PARSE_PROMPT_V3
            ]
            
            last_error = None
            last_content = ""
            
            for attempt, prompt_template in enumerate(prompts):
                try:
                    print(f"🤖 Chunk {chunk_id} - Attempt {attempt + 1}/{len(prompts)}...")
                    result, raw_content = await self._call_gemini(text, prompt_template)
                    last_content = raw_content
                    
                    if result and len(result) > 0:
                        print(f"✅ Chunk {chunk_id} - Extracted {len(result)} questions")
                        # Collect answers for cross-chunk matching
                        self._collect_answers(result)
                        return result
                    else:
                        print(f"⚠️ Chunk {chunk_id} - Attempt {attempt + 1}: Empty result")
                        
                except Exception as e:
                    last_error = e
                    print(f"❌ Chunk {chunk_id} - Attempt {attempt + 1} failed: {e}")
                    await asyncio.sleep(0.5)
            
            # Try to salvage from last response
            print(f"⚠️ Chunk {chunk_id} - Trying aggressive JSON extraction...")
            if last_content:
                result = self._aggressive_extract_json(last_content)
                if result:
                    print(f"✅ Chunk {chunk_id} - Salvaged {len(result)} questions")
                    self._collect_answers(result)
                    return result
            
            print(f"❌ Chunk {chunk_id} - Falling back to mock parser")
            return self._mock_parse(text)
    
    async def _call_gemini(self, text: str, prompt_template: str) -> tuple[List[Dict], str]:
        """Call Gemini API with JSON mode"""
        prompt = prompt_template.format(text=text)
        
        loop = asyncio.get_event_loop()
        
        def call_api():
            from google.genai import types
            try:
                response = self._client.models.generate_content(
                    model=self.gemini_model,
                    contents=[
                        types.Content(
                            role="user",
                            parts=[types.Part.from_text(prompt)]
                        )
                    ],
                    config=types.GenerateContentConfig(
                        system_instruction=self.SYSTEM_PROMPT,
                        temperature=0,
                        max_output_tokens=self.max_tokens,
                        response_mime_type="application/json",
                    )
                )
                return response.text
            except Exception as e:
                print(f"   JSON mode failed ({e}), trying without...")
                response = self._gemini_model.generate_content(
                    prompt,
                    generation_config={
                        "temperature": 0,
                        "max_output_tokens": self.max_tokens,
                    }
                )
                return response.text
        
        content = await loop.run_in_executor(None, call_api)
        
        if not content:
            return [], ""
        
        result = self._extract_json(content)
        return result, content
    
    async def _parse_chunked_parallel(self, text: str, progress_callback: Optional[Callable] = None) -> List[Dict[str, Any]]:
        """⚡ Parallel chunk processing with improved merging"""
        chunks = self._smart_chunk(text)
        total_chunks = len(chunks)
        print(f"📄 Split into {total_chunks} chunks (max {self.max_concurrency} parallel)")
        
        completed = [0]
        
        async def process_chunk(idx: int, chunk: str) -> tuple[int, List[Dict]]:
            start = time.time()
            result = await self._parse_single(chunk, chunk_id=idx)
            elapsed = time.time() - start
            
            completed[0] += 1
            print(f"✅ Chunk {idx + 1}/{total_chunks} done ({len(result)} questions, {elapsed:.1f}s)")
            
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
                print(f"❌ Chunk failed: {r}")
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
        
        print(f"✅ Total: {len(all_questions)} unique questions")
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
            print("⚠️ _extract_json: Content is empty")
            return []
        
        content = content.strip()
        
        # Check if truncated
        if not content.rstrip().endswith(']'):
            print(f"⚠️ JSON may be truncated. Last 100 chars: ...{content[-100:]}")
        
        # Pre-fix: Fix triple backslashes (common Gemini issue with LaTeX)
        content = re.sub(r'\\\\\\+', r'\\\\', content)
        
        # Method 1: Direct parse
        try:
            result = json.loads(content)
            if isinstance(result, list):
                print(f"✅ Direct parse success: {len(result)} items")
                return result
        except json.JSONDecodeError as e:
            print(f"⚠️ Direct parse failed: {e}")
        
        # Method 2: Remove markdown
        if "```json" in content:
            try:
                json_str = content.split("```json")[1].split("```")[0].strip()
                json_str = re.sub(r'\\\\\\+', r'\\\\', json_str)  # Fix triple backslash
                result = json.loads(json_str)
                if isinstance(result, list):
                    print(f"✅ Markdown parse success: {len(result)} items")
                    return result
            except Exception as e:
                print(f"⚠️ Markdown parse failed: {e}")
        
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
            print("⚠️ No '[' found in content")
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
                print(f"⚠️ Brackets unmatched, using last ']' at position {last_bracket}")
            else:
                print("⚠️ Could not find matching ']'")
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
                print(f"⚠️ Fix '{name}' failed: {e}")
        
        # Try to parse
        try:
            result = json.loads(current)
            if isinstance(result, list):
                print(f"✅ Aggressive parse success after fixes: {len(result)} items")
                return result
        except json.JSONDecodeError as e:
            print(f"⚠️ Parse after all fixes failed: {e}")
            print(f"⚠️ JSON snippet (first 500 chars): {current[:500]}")
        
        # Last resort: Try to extract individual objects
        print("⚠️ Trying to extract individual JSON objects...")
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
                        objects.append(obj)
                except:
                    pass
        
        if objects:
            print(f"✅ Extracted {len(objects)} individual objects")
        else:
            print("⚠️ Could not extract any individual objects")
        
        return objects


# ============ SPEED PRESETS ============

def create_fast_parser(**kwargs):
    """🚀 Fast: Larger chunks, more parallel"""
    return AIQuestionParser(
        gemini_model="gemini-2.5-pro",
        max_chunk_size=20000,
        max_concurrency=5,
        max_tokens=65536,
        **kwargs
    )

def create_balanced_parser(**kwargs):
    """⚖️ Balanced: Medium settings"""
    return AIQuestionParser(
        gemini_model="gemini-2.5-pro",
        max_chunk_size=15000,
        max_concurrency=3,
        max_tokens=65536,
        **kwargs
    )

def create_quality_parser(**kwargs):
    """🎯 Quality: Smaller chunks, more accurate"""
    return AIQuestionParser(
        gemini_model="gemini-2.5-pro",
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
        print(json.dumps(result, indent=2, ensure_ascii=False))
    
    asyncio.run(test())