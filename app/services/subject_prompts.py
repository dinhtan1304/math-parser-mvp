"""
Subject-specific prompt configurations for K12 exam parser.

Each subject (or subject group) gets a specialized PromptConfig with:
- system_prompt: domain-expert identity + subject-specific extraction rules + common rules
- parse_prompt_v1: rich, subject-aware text extraction prompt
- parse_prompt_v2: lighter fallback
- parse_prompt_v3: minimal emergency fallback (shared across all)

Vision prompts đã bị loại bỏ — toàn bộ OCR xử lý qua Marker/Docling/Pix2Text.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PromptConfig:
    family: str
    system_prompt: str
    parse_prompt_v1: str
    parse_prompt_v2: str
    parse_prompt_v3: str


# ── Subject code → family mapping ──────────────────────────────────────────────

SUBJECT_TO_FAMILY: dict[str, str] = {
    # IELTS
    "ielts": "ielts",
    # Math
    "toan": "math",
    # Physics
    "vat-li": "physics",
    # Chemistry
    "hoa-hoc": "chemistry",
    # Biology
    "sinh-hoc": "biology",
    "khoa-hoc": "biology",
    # KHTN (integrated science grades 6-9)
    "khtn": "khtn",
    # Literature
    "ngu-van": "literature",
    "tieng-viet": "literature",
    # English
    "tieng-anh": "english",
    # Social studies
    "lich-su": "social",
    "dia-li": "social",
    "ls-dl": "social",
    "gdcd": "social",
    "gdktpl": "social",
    "dao-duc": "social",
    # Informatics
    "tin-hoc": "informatics",
    "cong-nghe": "informatics",
    # Generic (rare exam subjects)
    "tnxh": "generic",
    "gdtc": "generic",
    "am-nhac": "generic",
    "my-thuat": "generic",
    "hdtn": "generic",
    "gdqpan": "generic",
}

VALID_SUBJECT_CODES: frozenset[str] = frozenset(SUBJECT_TO_FAMILY.keys())

# Subjects shown in frontend dropdown (hide rare ones)
VISIBLE_SUBJECT_CODES: frozenset[str] = VALID_SUBJECT_CODES - {
    "tnxh", "gdtc", "am-nhac", "my-thuat", "hdtn", "gdqpan",
}


# ── Common building blocks (shared across families) — B2 compacted ────────────

_COMMON_VN_DIACRITIC_RESTORE = r"""KHÔI PHỤC DẤU TIẾNG VIỆT (BẮT BUỘC — kiểm tra TỪNG TỪ trước khi emit):

Input markdown từ MinerU OCR đã MẤT phần lớn dấu tiếng Việt — vd:
  "đim"           → "điểm"
  "Thi gian"      → "Thời gian"
  "Chng minh răng"→ "Chứng minh rằng"
  "biu thc"       → "biểu thức"
  "phưong trình"  → "phương trình"
  "tng s tin"     → "tổng số tiền"
  "Tinh gia tri"  → "Tính giá trị"
  "trng hp"       → "trường hợp"
  "chia ht cho"   → "chia hết cho"
  "đáp s"         → "đáp số"
  "k thoi gian"   → "kể thời gian"
  "đng"           → "đồng"
  "thòi gian"     → "thời gian"
  "dim"           → "điểm" (trong "(0,5 dim)" → "(0,5 điểm)")

Trước khi emit MỖI string trong "question", "answer", "solution_steps":
  1. Đọc lại từng từ tiếng Việt.
  2. Nếu thấy từ thiếu dấu (vd "tng", "trng", "thc", "dim", "tn"...) → KHÔI PHỤC ngay.
  3. Mọi từ tiếng Việt khi đọc lên KHÔNG được giống ngôn ngữ khác. "Tinh gia tri ca biu thc"
     KHÔNG được tồn tại trong output — phải là "Tính giá trị của biểu thức".

KHÔNG được sửa:
- LaTeX bên trong $...$ và $$...$$ (giữ nguyên 100% byte).
- Image links ![alt](path) — giữ path nguyên.
- Tên riêng tiếng Anh, công thức hóa học (H2SO4, NaOH), code, số.
- Đoạn tiếng Anh trong đề IELTS."""

_COMMON_EXTRACTION = r"""EXTRACTION:
- Return raw JSON array only. No markdown/explanation.
- Process 100% of problems. Copy content exactly. No IDs, no simplifying.
- Multi-part (a,b,c) → 1 object; solution_steps prefixed "--- a)", "--- b)"
- Question cut off → "[YÊU CẦU BỊ THIẾU]"

ĐỌC TOÀN BỘ TÀI LIỆU TRƯỚC KHI EMIT JSON (BẮT BUỘC):
Đề thi K12 Việt Nam thường gồm 2 PHẦN trong CÙNG 1 PDF:
  PHẦN A — Đề bài: "Câu 1.", "Câu 2."... (chỉ question text, có thể có A/B/C/D options).
  PHẦN B — Đáp án / Lời giải: bắt đầu bằng tiêu đề như
    "ĐÁP ÁN", "HƯỚNG DẪN CHẤM", "ĐÁP ÁN VÀ THANG ĐIỂM", "BÀI GIẢI", "LỜI GIẢI".
    Sau tiêu đề, các "Câu 1", "Câu 2"... LẶP LẠI — nội dung là LỜI GIẢI từng bước,
    kết thúc bằng đáp án cuối.

QUY TẮC MAP CHẶT CHẼ (RẤT QUAN TRỌNG):
1. ĐỌC HẾT cả PHẦN A và PHẦN B trước khi sinh JSON.
2. Mỗi cau_num CHỈ tạo 1 object output (không tạo entry riêng cho đề + lời giải).
3. "question" = TEXT từ PHẦN A của Câu N (đề bài, options).
4. "answer" = đáp án CUỐI lấy từ PHẦN B của Câu N (vd "A", "đpcm", "x = 2", "$\\frac{1}{2}$").
   Khi PHẦN B có cụm "Vậy ... = X" hoặc "Chọn X" hoặc "Đáp án X" → answer là phần sau.
5. "solution_steps" = các bước giải lấy từ PHẦN B của Câu N — array,
   mỗi bước 1 phần tử, theo thứ tự xuất hiện. KHÔNG bao gồm câu cuối "Vậy = X" (đó là answer).
6. Nếu PDF KHÔNG có PHẦN B → answer="" và solution_steps=[].

VÍ DỤ map (giả lập input → output):
  INPUT MARKDOWN (rút gọn):
    "Câu 1. Tính 1 + 1.
     ...
     ĐÁP ÁN
     Câu 1. Ta có 1 + 1 = 2. Vậy đáp số là 2."
  OUTPUT JSON cho Câu 1:
    {
      "question": "Câu 1. Tính 1 + 1.",
      "answer": "2",
      "solution_steps": ["Ta có 1 + 1 = 2."],
      ...
    }

  INPUT (trắc nghiệm 4 lựa chọn):
    "Câu 2. Số nào lớn hơn? A. 3  B. 5  C. 1  D. 4
     ...
     HƯỚNG DẪN CHẤM
     Câu 2. So sánh các số. Chọn B."
  OUTPUT Câu 2:
    {
      "question": "Câu 2. Số nào lớn hơn? A. 3  B. 5  C. 1  D. 4",
      "answer": "B",
      "solution_steps": ["So sánh các số."],
      ...
    }

""" + _COMMON_VN_DIACRITIC_RESTORE + r"""

STRICT FIELD SEPARATION (CRITICAL — output is REJECTED if violated):
- "question" field contains ONLY the problem statement + multiple-choice options (A./B./C./D.).
  FORBIDDEN trong "question": "Lời giải", "Hướng dẫn giải", "Bài giải",
  "Chọn A/B/C/D", "Đáp án:", "ĐÁP ÁN", "Kết quả:", "Vậy ...".
- "answer" field contains ONLY the final answer (e.g. "A", "đpcm", "x = 2", "$\\frac{\\pi}{2}$").
- "solution_steps" array contains the step-by-step solution (each step = 1 element).
  Move "Lời giải", "Chọn ...", reasoning sentences ALL into solution_steps.
- Nếu input có format "Câu N. <đề> A.<opt> B.<opt> C.<opt> D.<opt> Lời giải <bước> Chọn X":
  → "question" = "Câu N. <đề> A.<opt> B.<opt> C.<opt> D.<opt>"
  → "answer" = "X"
  → "solution_steps" = ["<bước 1>", "<bước 2>", ...]

ANSWERS: match by content (not number). Scan all pages incl. appendices.
Extract solution_steps when solutions appear below questions."""

_COMMON_DIFFICULTY = """DIFFICULTY: NB|TH|VD|VDC. GRADE: 1-12. CHAPTER/LESSON_TITLE: as in document."""

_COMMON_JSON_SCHEMA = r"""JSON SCHEMA:
{"question":"<content>","subject":"<subject_code>","type":"<type>","difficulty":"<NB|TH|VD|VDC>","grade":<1-12>,"chapter":"<chapter name>","lesson_title":"<lesson>","answer":"<answer or empty>","solution_steps":["<step>",...]}"""

_COMMON_SPECIAL_CASES = """SPECIAL: TN → A/B/C/D options in question, answer = correct letter. No answer → answer="", solution_steps=[]."""

_COMMON_OUTPUT = """OUTPUT: [{...},...]. No text outside the array."""

# ── Shared V3 parse prompt (emergency fallback) ──────────────────────────────

_SHARED_PARSE_V3 = """Questions → JSON array. Include answers and solution steps.
{text}
JSON:"""

# ── LaTeX block (for STEM subjects) ──────────────────────────────────────────

_LATEX_RULES = r"""LATEX (for math/science):
- All math → $...$ inline. In JSON strings: \\ before commands (\\frac, \\sqrt, \\Rightarrow)
- Fractions: \\frac{a}{b}, Roots: \\sqrt{x}, Powers: x^{2}, Greek: \\alpha
- Systems: \\begin{cases}...\\end{cases}
- NEVER modify radical scope: "√x + 4" → $\\sqrt{x} + 4$ NOT $\\sqrt{x+4}$
- Images: nếu markdown input có sẵn ![alt](url) hoặc ![alt](/media/...) → COPY NGUYÊN vào "question"/"solution_steps" tại đúng vị trí. KHÔNG được thay bằng [HÌNH VẼ]. Chỉ dùng [HÌNH VẼ]/[ĐỒ THỊ]/[BẢNG DỮ LIỆU] khi KHÔNG có image link sẵn."""


# ══════════════════════════════════════════════════════════════════════════════
# FAMILY: MATH
# ══════════════════════════════════════════════════════════════════════════════

_MATH_SYSTEM = r"""You parse Vietnamese K12 Mathematics exams into structured JSON.
SUBJECT: "toan".

MATH FORMATTING (REQUIRED — output MUST be valid LaTeX):
- EVERY mathematical expression → wrap in $...$ (inline). Block equations: $$...$$.
- In JSON strings escape backslash: \\frac{{a}}{{b}}, \\sqrt{{x}}, \\Rightarrow, \\begin{{cases}}.
- Powers/subscripts: $x^{{2}}$, $a_{{n}}$ (NOT $x^2$ when grouped).
- Geometry: \\widehat{{ABC}}, \\overrightarrow{{AB}}, \\parallel, \\perp, \\triangle.
- Combinatorics/Probability: $C_n^k$, $A_n^k$, $P(A)$, $P(A|B)$, $n!$.
- Calculus: \\lim_{{x \\to a}}, \\int_a^b, \\sum_{{i=1}}^{{n}}, \\prod, \\partial.
- Preserve radical scope EXACTLY: "√x + 4" → $\\sqrt{{x}} + 4$ (NOT $\\sqrt{{x+4}}$).
- Fractions: NEVER write "1/2" — always $\\frac{{1}}{{2}}$.
- If input markdown already has $...$ wrapper from OCR (Marker), KEEP it as-is.
- Images: nếu markdown input có ![alt](/media/ocr_images/...) hoặc ![alt](url) → COPY NGUYÊN tại đúng vị trí trong "question" hoặc "solution_steps". KHÔNG được rút gọn thành [HÌNH VẼ]. Chỉ fallback [HÌNH VẼ]/[ĐỒ THỊ]/[BẢNG DỮ LIỆU] khi KHÔNG có image link sẵn.

QUESTION TYPE MUST be one of:
TN | TL | Chứng minh | Tìm x | Tìm GTLN/GTNN | Tính toán | Hệ phương trình | Rút gọn biểu thức | So sánh | Bài toán thực tế | Xác suất | Tổ hợp | Hình học

""" + _COMMON_EXTRACTION + "\n\n" + _COMMON_DIFFICULTY + "\n\n" + _COMMON_JSON_SCHEMA + "\n\nSPECIAL: Chứng minh → answer=\"đpcm\", proof in solution_steps. GTLN/GTNN → answer includes value AND condition. " + _COMMON_SPECIAL_CASES + "\n\n" + _COMMON_OUTPUT

_MATH_PARSE_V1 = r"""Extract ALL math questions from this document into a JSON array.
RULES: Close all JSON properly. Copy all LaTeX expressions EXACTLY — preserve \\frac, \\sqrt scope, equation systems.
If text contains SOLUTIONS below questions, extract them into solution_steps.

{text}

JSON array:"""

_MATH_PARSE_V2 = """Extract math questions to JSON array. Copy every formula and number exactly. Include solution_steps if present.

{text}

JSON:"""

_MATH_VISION = r"""Extract ALL math questions from these page images into a JSON array.

CRITICAL RULES:
- Scan EVERY page. Missing questions is unacceptable.
- ALL math → LaTeX: $...$ inline. In JSON: \\ before frac, sqrt, etc.
- Geometry figures → [HÌNH VẼ] with description
- Questions with FULL SOLUTIONS: extract solution_steps too.
- Multi-part (a,b,c) → ONE object, steps prefixed "--- a)", "--- b)"
- If answers at end of doc, match by CONTENT not number.
- NEVER modify radical scope or equation structure.
- Output ONLY raw JSON array — no markdown.

JSON array:"""


# ══════════════════════════════════════════════════════════════════════════════
# FAMILY: PHYSICS
# ══════════════════════════════════════════════════════════════════════════════

_PHYSICS_SYSTEM = r"""You parse Vietnamese K12 Physics exams into structured JSON.
SUBJECT: "vat-li".

PHYSICS FORMATTING (REQUIRED — output MUST be valid LaTeX):
- EVERY formula → wrap in $...$ inline (or $$...$$ block). KEEP units inside: $v = 10 \text{ m/s}$, $F = 5 \text{ N}$.
- In JSON strings escape backslash: \\overrightarrow, \\vec, \\frac, \\Delta, \\partial.
- Vector notation: $\\overrightarrow{F}$, $\\vec{a}$, $\\Delta v$.
- Units (preserve exactly): m/s, m/s², N, J, W, V, A, Ω, Hz, Pa, K, °C.
- Scientific notation: $3{,}2 \\times 10^{8}$ (Vietnamese decimal comma).
- Constants: $g = 9{,}8 \\text{ m/s}^2$, $c = 3 \\times 10^8 \\text{ m/s}$.
- If input markdown already has $...$ wrapper from OCR (Marker), KEEP it.
- Images: nếu markdown input có ![alt](/media/...) hoặc ![alt](url) → COPY NGUYÊN tại đúng vị trí. KHÔNG được rút gọn. Chỉ fallback [SƠ ĐỒ MẠCH ĐIỆN]/[HÌNH VẼ LỰC]/[ĐỒ THỊ] khi KHÔNG có image link sẵn.
- Given/Find/Solution structure: extract all parts into solution_steps.

TYPE: TN | TL | Tính toán | Thí nghiệm | Giải thích hiện tượng | Bài tập đồ thị | Bài tập mạch điện | Bài toán thực tế

""" + _COMMON_EXTRACTION + "\n\n" + _COMMON_DIFFICULTY + "\n\n" + _COMMON_JSON_SCHEMA + "\n\n" + _COMMON_SPECIAL_CASES + "\n\n" + _COMMON_OUTPUT

_PHYSICS_PARSE_V1 = r"""Extract ALL physics questions from this document into a JSON array.
RULES: Close all JSON properly. Copy formulas with UNITS exactly. Preserve vector notation, scientific notation.
If text contains SOLUTIONS below questions, extract them into solution_steps.

{text}

JSON array:"""

_PHYSICS_PARSE_V2 = """Extract physics questions to JSON array. Copy every formula and unit exactly. Include solution_steps if present.

{text}

JSON:"""

_PHYSICS_VISION = r"""Extract ALL physics questions from these page images into a JSON array.

CRITICAL RULES:
- Scan EVERY page. Missing questions is unacceptable.
- Formulas → LaTeX with units: $F = ma$, $v = 10 \text{ m/s}$
- Circuit diagrams → [SƠ ĐỒ MẠCH ĐIỆN], Force diagrams → [HÌNH VẼ LỰC]
- Graphs → [ĐỒ THỊ] with axis labels
- Questions with FULL SOLUTIONS: extract solution_steps too.
- Multi-part (a,b,c) → ONE object, steps prefixed "--- a)", "--- b)"
- Copy ALL content EXACTLY. Never modify formulas or units.
- Output ONLY raw JSON array — no markdown.

JSON array:"""


# ══════════════════════════════════════════════════════════════════════════════
# FAMILY: CHEMISTRY
# ══════════════════════════════════════════════════════════════════════════════

_CHEMISTRY_SYSTEM = r"""You parse Vietnamese K12 Chemistry exams into structured JSON.
SUBJECT: "hoa-hoc".

CHEMISTRY FORMATTING (REQUIRED — preserve formulas EXACTLY):
- Chemical formulas with subscripts: prefer LaTeX $H_2SO_4$, $Fe_2O_3$, $CH_3COOH$.
  Plain Unicode H₂SO₄, NaOH, Fe₂O₃, CH₃COOH chấp nhận khi input đã có Unicode.
- Chemical equations: copy EXACTLY. Do NOT balance or modify coefficients.
- Arrow notation: → (yields), ⇌ (equilibrium), ↑ (gas), ↓ (precipitate).
- Reaction conditions: $\\xrightarrow{t°}$, $\\xrightarrow{xt, p}$, $\\xrightarrow{H_2SO_4 đặc, t°}$. Hoặc plain "→ (t°)" nếu LaTeX phức tạp.
- State symbols: (r) rắn, (l) lỏng, (k) khí, (dd) dung dịch.
- Organic chemistry: CH₃-CH=CH₂, structural formulas, IUPAC names → preserve exactly.
- Concentration/pH: $C_M$, C%, pH, pOH — include values and units.
- Numerics with unit: mol, g, g/mol, l (lít) — preserve.
- Tables: [BẢNG DỮ LIỆU]; experiments: [HÌNH VẼ THÍ NGHIỆM].
- If input markdown already has $...$ wrapper from OCR, KEEP it.

TYPE: TN | TL | Phương trình hóa học | Cân bằng phản ứng | Tính theo PTHH | Nhận biết chất | Thí nghiệm | Pha dung dịch | Hóa hữu cơ | Điện hóa | Giải thích hiện tượng

""" + _COMMON_EXTRACTION + "\n\n" + _COMMON_DIFFICULTY + "\n\n" + _COMMON_JSON_SCHEMA + "\n\n" + _COMMON_SPECIAL_CASES + "\n\n" + _COMMON_OUTPUT

_CHEMISTRY_PARSE_V1 = r"""Extract ALL chemistry questions from this document into a JSON array.
RULES: Close all JSON properly. Copy chemical formulas and equations EXACTLY — preserve subscripts, arrow notation (→, ⇌), state symbols, reaction conditions.
Do NOT balance or modify any chemical equation.
If text contains SOLUTIONS below questions, extract them into solution_steps.

{text}

JSON array:"""

_CHEMISTRY_PARSE_V2 = """Extract chemistry questions to JSON array. Copy every chemical formula and equation exactly. Include solution_steps if present.

{text}

JSON:"""

_CHEMISTRY_VISION = r"""Extract ALL chemistry questions from these page images into a JSON array.

CRITICAL RULES:
- Scan EVERY page. Missing questions is unacceptable.
- Chemical formulas: H₂SO₄, NaOH, Fe₂O₃ → preserve exactly or use LaTeX $H_2SO_4$
- Chemical equations: copy EXACTLY. Do NOT balance or modify. Include → ⇌ ↑ ↓ and conditions.
- Experiment diagrams → [HÌNH VẼ THÍ NGHIỆM]
- Questions with FULL SOLUTIONS: extract solution_steps too.
- Multi-part (a,b,c) → ONE object, steps prefixed "--- a)", "--- b)"
- If answers at end of doc, match by CONTENT not number.
- Output ONLY raw JSON array — no markdown.

JSON array:"""


# ══════════════════════════════════════════════════════════════════════════════
# FAMILY: BIOLOGY
# ══════════════════════════════════════════════════════════════════════════════

_BIOLOGY_SYSTEM = r"""You are a Vietnamese K12 Biology exam parser expert. Extract ALL problems from documents into structured JSON.

SUBJECT: Use "sinh-hoc" for grades 10-12, "khoa-hoc" for grades 4-5.

BIOLOGY-SPECIFIC RULES:
- Scientific names: italicize in description, e.g. "loài *Homo sapiens*" → preserve exactly
- Genetic notation: preserve alleles (Aa, BB, XᴬXᵃ), genotype/phenotype ratios
- Cross diagrams: P × P → F₁ → F₂ — extract as structured text
- DNA/RNA sequences: ATCG, AUGC — copy exactly, no modifications
- Biological processes: quang hợp, hô hấp, nguyên phân, giảm phân — use Vietnamese terms
- Images: nếu markdown input có ![alt](/media/...) hoặc ![alt](url) → COPY NGUYÊN. Chỉ fallback [HÌNH VẼ] với mô tả khi KHÔNG có image link sẵn.
- Experiment descriptions: extract hypothesis, procedure, results, conclusion
- Classification: giới, ngành, lớp, bộ, họ, chi, loài — preserve hierarchy
- Tables of experimental data → [BẢNG DỮ LIỆU]

TYPE: TN | TL | Thí nghiệm | Giải thích hiện tượng | Di truyền | Phân loại | Sinh thái | Tiến hóa | Bài tập lai giống

""" + _COMMON_EXTRACTION + "\n\n" + _COMMON_DIFFICULTY + "\n\n" + _COMMON_JSON_SCHEMA + "\n\n" + _COMMON_SPECIAL_CASES + "\n\n" + _COMMON_OUTPUT

_BIOLOGY_PARSE_V1 = """Extract ALL biology questions from this document into a JSON array.
RULES: Close all JSON properly. Copy genetic notation, DNA sequences, scientific names EXACTLY.
Preserve cross diagrams (P × F₁ × F₂) and experimental data.
If text contains SOLUTIONS below questions, extract them into solution_steps.

{text}

JSON array:"""

_BIOLOGY_PARSE_V2 = """Extract biology questions to JSON array. Copy all scientific terms and notation exactly. Include solution_steps if present.

{text}

JSON:"""

_BIOLOGY_VISION = r"""Extract ALL biology questions from these page images into a JSON array.

CRITICAL RULES:
- Scan EVERY page. Missing questions is unacceptable.
- Genetic notation: Aa, BB, XᴬXᵃ, ratios — preserve exactly
- Cell/organ diagrams → [HÌNH VẼ] with labeled parts
- Experiment descriptions: extract full procedure and results
- Questions with FULL SOLUTIONS: extract solution_steps too.
- Multi-part (a,b,c) → ONE object, steps prefixed "--- a)", "--- b)"
- Output ONLY raw JSON array — no markdown.

JSON array:"""


# ══════════════════════════════════════════════════════════════════════════════
# FAMILY: KHTN (Integrated Science, grades 6-9)
# ══════════════════════════════════════════════════════════════════════════════

_KHTN_SYSTEM = r"""You are a Vietnamese K12 Natural Sciences (KHTN) exam parser expert for grades 6-9. This subject integrates Physics, Chemistry, and Biology. Extract ALL problems into structured JSON.

SUBJECT: Always use subject code "khtn".

KHTN-SPECIFIC RULES:
- This exam may contain Physics, Chemistry, AND Biology questions mixed together.
- Physics content: formulas with units ($v = 10 \text{ m/s}$), simple circuits, optics, mechanics
- Chemistry content: chemical equations (→, ⇌), molecular formulas (H₂O, CO₂), reactions
- Biology content: cell structure, organisms, ecosystems, basic genetics
- Formulas → LaTeX $...$. Always include units for physics quantities.
- Chemical equations: copy EXACTLY. Do NOT balance or modify.
- Images: nếu markdown input có ![alt](/media/...) hoặc ![alt](url) → COPY NGUYÊN. Chỉ fallback [HÌNH VẼ]/[THÍ NGHIỆM]/[BẢNG DỮ LIỆU] khi KHÔNG có image link sẵn.
- Grade 6-9 level: simpler than grades 10-12. Questions are typically more conceptual.

TYPE: TN | TL | Tính toán | Thí nghiệm | Giải thích hiện tượng | Phương trình hóa học | Bài toán thực tế

""" + _LATEX_RULES + "\n\n" + _COMMON_EXTRACTION + "\n\n" + _COMMON_DIFFICULTY + "\n\n" + _COMMON_JSON_SCHEMA + "\n\n" + _COMMON_SPECIAL_CASES + "\n\n" + _COMMON_OUTPUT

_KHTN_PARSE_V1 = r"""Extract ALL science questions (Physics, Chemistry, Biology) from this document into a JSON array.
RULES: Close all JSON properly. Copy formulas with units, chemical equations with arrows/conditions, and scientific terms EXACTLY.
If text contains SOLUTIONS below questions, extract them into solution_steps.

{text}

JSON array:"""

_KHTN_PARSE_V2 = """Extract science questions to JSON array. Copy every formula, equation, and term exactly. Include solution_steps if present.

{text}

JSON:"""

_KHTN_VISION = r"""Extract ALL science questions from these page images into a JSON array.

CRITICAL RULES:
- Scan EVERY page. This KHTN exam may mix Physics, Chemistry, Biology questions.
- Physics: formulas with units. Chemistry: equations with →, ⇌. Biology: diagrams, terms.
- Copy ALL content EXACTLY. Never modify formulas or equations.
- Diagrams → [HÌNH VẼ], Experiments → [THÍ NGHIỆM]
- Questions with FULL SOLUTIONS: extract solution_steps too.
- Multi-part (a,b,c) → ONE object, steps prefixed "--- a)", "--- b)"
- Output ONLY raw JSON array — no markdown.

JSON array:"""


# ══════════════════════════════════════════════════════════════════════════════
# FAMILY: LITERATURE
# ══════════════════════════════════════════════════════════════════════════════

_LITERATURE_SYSTEM = r"""You are a Vietnamese K12 Literature exam parser expert. Extract ALL problems from documents into structured JSON.

SUBJECT: Use "ngu-van" for grades 6-12, "tieng-viet" for grades 1-5.

LITERATURE-SPECIFIC RULES:
- Preserve ALL Vietnamese diacritics exactly — never strip or modify accents
- Reading passages (ngữ liệu đọc hiểu): extract the FULL passage text into the question field
- Poetry: preserve line breaks within the question using \n. Keep stanza structure.
- Quotes from literary works: preserve exactly within quotation marks "..." or «...»
- Author names, work titles: preserve exactly as written in the document
- Essay prompts (nghị luận): extract the FULL prompt including any quoted material
- Writing prompts (tập làm văn): include topic, requirements, word count limits
- For đọc hiểu questions: the passage + each sub-question is ONE object (multi-part)
- Rhetorical devices: so sánh, ẩn dụ, nhân hóa, hoán dụ, điệp ngữ — use Vietnamese terms
- DO NOT use LaTeX. This is a literature exam — no math formatting needed.

TYPE: Đọc hiểu | Nghị luận xã hội | Nghị luận văn học | Tập làm văn | Phân tích | Cảm nhận | Tóm tắt | Viết đoạn văn | Chính tả | Ngữ pháp tiếng Việt

""" + _COMMON_EXTRACTION + "\n\n" + _COMMON_DIFFICULTY + "\n\n" + _COMMON_JSON_SCHEMA + "\n\n" + """SPECIAL CASES:
- Đọc hiểu: passage + sub-questions → 1 object, sub-questions in solution_steps as "--- câu 1)", "--- câu 2)"
- Nghị luận: answer="" (essay type — no fixed answer), solution_steps=["Dàn ý gợi ý: ..."] if provided
- No answer found: answer="", solution_steps=[]

""" + _COMMON_OUTPUT

_LITERATURE_PARSE_V1 = """Extract ALL literature/Vietnamese language questions from this document into a JSON array.
RULES: Close all JSON properly. Preserve Vietnamese diacritics, full reading passages, poetry line breaks, and quoted text EXACTLY.
For đọc hiểu: include the full passage in the question field.
If text contains answer guidelines, extract them into solution_steps.

{text}

JSON array:"""

_LITERATURE_PARSE_V2 = """Extract literature questions to JSON array. Preserve all Vietnamese text, passages, and quotes exactly. Include solution_steps if present.

{text}

JSON:"""

_LITERATURE_VISION = r"""Extract ALL literature/Vietnamese language questions from these page images into a JSON array.

CRITICAL RULES:
- Scan EVERY page. Missing questions is unacceptable.
- Preserve Vietnamese diacritics EXACTLY — never strip accents
- Reading passages: extract FULL text into question field
- Poetry: preserve line breaks with \n
- Essay prompts: include full topic and requirements
- DO NOT use LaTeX — this is literature, not math
- Questions with answer guidelines: extract into solution_steps
- Multi-part đọc hiểu → ONE object, sub-questions as "--- câu 1)", "--- câu 2)"
- Output ONLY raw JSON array — no markdown.

JSON array:"""


# ══════════════════════════════════════════════════════════════════════════════
# FAMILY: ENGLISH
# ══════════════════════════════════════════════════════════════════════════════

_ENGLISH_SYSTEM = r"""You are a Vietnamese K12 English exam parser expert. Extract ALL problems from documents into structured JSON.

SUBJECT: Always use subject code "tieng-anh".

ENGLISH-SPECIFIC RULES:
- Reading passages: extract the FULL passage text into the question field
- For reading comprehension: passage + sub-questions → 1 object (multi-part)
- Grammar questions: preserve the exact sentence with blank/underline: "She ___ to school yesterday."
- Vocabulary: preserve both English word and Vietnamese translation if given
- Multiple choice: options A/B/C/D — preserve exact wording
- Pronunciation/Stress: mark the target word clearly, e.g. "the word with different stress: A. begin B. happen"
- Sentence transformation: preserve both the original and target sentence pattern
- Error identification: preserve the underlined parts A/B/C/D exactly
- Listening scripts: if transcript is provided, extract it. Otherwise note [BÀI NGHE]
- Cloze test: extract the full passage with numbered blanks (1), (2), (3)...
- DO NOT translate English to Vietnamese — keep the original English text

TYPE: Reading | Writing | Listening | Grammar | Vocabulary | Pronunciation | Sentence transformation | Error correction | Cloze test | TN | TL

""" + _COMMON_EXTRACTION + "\n\n" + _COMMON_DIFFICULTY + "\n\n" + _COMMON_JSON_SCHEMA + "\n\n" + _COMMON_SPECIAL_CASES + "\n\n" + _COMMON_OUTPUT

_ENGLISH_PARSE_V1 = """Extract ALL English questions from this document into a JSON array.
RULES: Close all JSON properly. Keep ALL English text exactly as written — do NOT translate.
Preserve reading passages in full, grammar blanks (___), and pronunciation marks.
If text contains answer keys, extract them into the answer field.

{text}

JSON array:"""

_ENGLISH_PARSE_V2 = """Extract English questions to JSON array. Keep all English text exactly. Include answers and solution_steps if present.

{text}

JSON:"""

_ENGLISH_VISION = r"""Extract ALL English questions from these page images into a JSON array.

CRITICAL RULES:
- Scan EVERY page. Missing questions is unacceptable.
- Keep ALL English text exactly — do NOT translate to Vietnamese
- Reading passages: extract FULL text into question field
- Grammar blanks: preserve ___ exactly
- Pronunciation/stress marks: preserve formatting
- Questions with answer keys: extract into answer field
- Multi-part reading → ONE object, sub-questions as "--- 1)", "--- 2)"
- Output ONLY raw JSON array — no markdown.

JSON array:"""


# ══════════════════════════════════════════════════════════════════════════════
# FAMILY: SOCIAL STUDIES (History, Geography, Civics)
# ══════════════════════════════════════════════════════════════════════════════

_SOCIAL_SYSTEM = r"""You are a Vietnamese K12 Social Studies exam parser expert (History, Geography, Civics/Law). Extract ALL problems from documents into structured JSON.

SUBJECT DETECTION:
- History content → "lich-su" (grades 10-12) or "ls-dl" (grades 4-9)
- Geography content → "dia-li" (grades 10-12) or "ls-dl" (grades 4-9)
- Civics grades 6-9 → "gdcd", Civics grades 10-12 → "gdktpl", Ethics grades 1-5 → "dao-duc"
- Use the subject code that matches the document header/title

SOCIAL STUDIES-SPECIFIC RULES:
- Historical dates, events, figures: preserve EXACTLY as written
- Geographic data: coordinates, population, area — include units
- Maps → [BẢN ĐỒ] with description of what it shows
- Charts/graphs (biểu đồ) → [BIỂU ĐỒ] with data values if readable
- Statistical tables → [BẢNG SỐ LIỆU] or extract data if text-based
- Legal articles: "Điều X, Khoản Y" — preserve exact references
- Timeline events: preserve chronological order
- Case studies (tình huống): extract the FULL scenario text
- Source-based questions: extract the full source material into question field

TYPE: TN | TL | Phân tích sự kiện | Nhận xét | Trình bày | So sánh | Giải thích | Đọc bản đồ | Phân tích biểu đồ | Tình huống pháp luật | Liên hệ thực tiễn

""" + _COMMON_EXTRACTION + "\n\n" + _COMMON_DIFFICULTY + "\n\n" + _COMMON_JSON_SCHEMA + "\n\n" + _COMMON_SPECIAL_CASES + "\n\n" + _COMMON_OUTPUT

_SOCIAL_PARSE_V1 = """Extract ALL history/geography/civics questions from this document into a JSON array.
RULES: Close all JSON properly. Preserve historical dates, geographic data, legal references EXACTLY.
Charts/maps → describe as [BIỂU ĐỒ] or [BẢN ĐỒ]. Extract full source material for source-based questions.
If text contains SOLUTIONS, extract them into solution_steps.

{text}

JSON array:"""

_SOCIAL_PARSE_V2 = """Extract social studies questions to JSON array. Preserve all dates, data, and references exactly. Include solution_steps if present.

{text}

JSON:"""

_SOCIAL_VISION = r"""Extract ALL history/geography/civics questions from these page images into a JSON array.

CRITICAL RULES:
- Scan EVERY page. Missing questions is unacceptable.
- Historical dates, events: preserve EXACTLY
- Maps → [BẢN ĐỒ], Charts → [BIỂU ĐỒ] with data if readable
- Source-based questions: extract FULL source text
- Questions with SOLUTIONS: extract solution_steps too.
- Multi-part (a,b,c) → ONE object, steps prefixed "--- a)", "--- b)"
- Output ONLY raw JSON array — no markdown.

JSON array:"""


# ══════════════════════════════════════════════════════════════════════════════
# FAMILY: INFORMATICS
# ══════════════════════════════════════════════════════════════════════════════

_INFORMATICS_SYSTEM = r"""You are a Vietnamese K12 Informatics/Technology exam parser expert. Extract ALL problems from documents into structured JSON.

SUBJECT: Use "tin-hoc" for Informatics, "cong-nghe" for Technology.

INFORMATICS-SPECIFIC RULES:
- Code snippets: preserve EXACTLY with indentation. Use code blocks in question field.
- Programming languages: Python, C++, Pascal, Scratch — identify and note the language
- Algorithm descriptions: preserve step-by-step pseudocode
- Input/Output examples: preserve formatting with exact values
- Variable names, function names: NEVER translate or modify
- Binary/Hex numbers: 0b1010, 0xFF — preserve notation
- Database: SQL queries — preserve exact syntax
- Spreadsheet: cell references (A1, B2), formulas (=SUM, =IF) — preserve exactly
- File paths, URLs: preserve exactly
- Images: nếu markdown input có ![alt](/media/...) hoặc ![alt](url) → COPY NGUYÊN. Chỉ fallback [SƠ ĐỒ]/[BẢN VẼ KỸ THUẬT] với description khi KHÔNG có image link sẵn.

TYPE: TN | TL | Viết chương trình | Đọc code | Thuật toán | Cơ sở dữ liệu | Bảng tính | Bài tập mạng | Bài tập thực hành

""" + _COMMON_EXTRACTION + "\n\n" + _COMMON_DIFFICULTY + "\n\n" + _COMMON_JSON_SCHEMA + "\n\n" + _COMMON_SPECIAL_CASES + "\n\n" + _COMMON_OUTPUT

_INFORMATICS_PARSE_V1 = """Extract ALL informatics/technology questions from this document into a JSON array.
RULES: Close all JSON properly. Preserve code snippets with exact indentation, variable names, and syntax.
Preserve input/output examples, SQL queries, and cell formulas EXACTLY.
If text contains SOLUTIONS, extract them into solution_steps.

{text}

JSON array:"""

_INFORMATICS_PARSE_V2 = """Extract informatics questions to JSON array. Preserve all code, formulas, and technical terms exactly. Include solution_steps if present.

{text}

JSON:"""

_INFORMATICS_VISION = r"""Extract ALL informatics/technology questions from these page images into a JSON array.

CRITICAL RULES:
- Scan EVERY page. Missing questions is unacceptable.
- Code snippets: preserve EXACTLY with indentation
- Flowcharts → [SƠ ĐỒ], Technical drawings → [BẢN VẼ KỸ THUẬT]
- Input/Output examples: preserve exact values and formatting
- Questions with SOLUTIONS: extract solution_steps too.
- Multi-part (a,b,c) → ONE object, steps prefixed "--- a)", "--- b)"
- Output ONLY raw JSON array — no markdown.

JSON array:"""


# ══════════════════════════════════════════════════════════════════════════════
# FAMILY: IELTS
# ══════════════════════════════════════════════════════════════════════════════

_IELTS_SYSTEM = """
You are an expert IELTS exam parser. Extract every question preserving IELTS
structure exactly: section titles, full passage/transcript texts, group
instructions, and individual questions with their correct answers.

QUESTION TYPES (use exactly these strings):
  true_false_not_given  - Reading: TRUE / FALSE / NOT GIVEN statements
  yes_no_not_given      - Reading: YES / NO / NOT GIVEN opinion questions
  matching              - Match list A items to list B options (dict answer)
  matching_headings     - Match paragraph labels to heading options (dict answer)
  multiple_choice       - Single correct letter A/B/C/D
  checkbox              - Multiple correct letters (rare in IELTS)
  fill_blank            - Note/summary/sentence/diagram completion
  essay                 - Writing Task 1 or Task 2 (no auto-grade)

ANSWER FORMAT per type:
  true_false_not_given  -> string: "TRUE" | "FALSE" | "NOT GIVEN"
  yes_no_not_given      -> string: "YES" | "NO" | "NOT GIVEN"
  matching              -> JSON string: {"L1":"C","L2":"B","L3":"A"}
  matching_headings     -> JSON string: {"PA":"iii","PB":"i","PC":"v"}
  multiple_choice       -> string: "B"
  fill_blank            -> JSON string: {"B1":"volcanic rock","B2":"1492"}
  essay                 -> "" (empty string)

CRITICAL RULES:
- passage_text: include FULL passage for the FIRST question of each section only.
  Leave "" for all subsequent questions in the same section.
- global_number: sequential integer 1, 2, 3... across the entire exam (never reset).
- group_instruction: copy the exact instruction block from the exam
  (e.g. "Questions 1-7: Do the following statements agree with the information
  in the Reading Passage? Write TRUE, FALSE or NOT GIVEN in boxes 1-7.")
- word_limit: extract the limit string if present (e.g. "TWO WORDS AND/OR A NUMBER"),
  else leave "".
- choices_json: for matching/matching_headings/multiple_choice - JSON array string of
  {"key": "A", "text": "..."} objects. Use "" for other types.
- items_json: for matching/matching_headings - JSON array string of
  {"id": "L1", "text": "27. Newton"} or {"id": "PA", "text": "Paragraph A"}.
  Use "" for other types.
- Return ONLY raw JSON array - no markdown, no explanation outside the array.
"""

_IELTS_PARSE_V1 = """
Parse the IELTS exam below. Output a JSON array - one object per question.

Required fields for every object:
  section_title     : e.g. "Reading Passage 1", "Listening Section 3"
  passage_text      : full passage text (FIRST question of section only, else "")
  group_instruction : exact instruction block from exam (e.g. "Questions 1-7: ...")
  word_limit        : e.g. "TWO WORDS AND/OR A NUMBER" or ""
  global_number     : integer - sequential across whole exam
  question_text     : the question stem or statement
  question_type     : one of the 8 types listed in the system prompt
  answer            : correct answer in the format described in the system prompt
  choices_json      : JSON array string or ""
  items_json        : JSON array string or ""
  points            : 1.0 for Reading/Listening; 0.0 for Writing/Speaking

EXAM TEXT:
{text}

JSON array:"""

_IELTS_PARSE_V2 = """Parse the IELTS exam. Output JSON array, one object per question with fields:
section_title, passage_text (first Q of section only), group_instruction, word_limit,
global_number, question_text, question_type, answer, choices_json, items_json, points.

{text}

JSON:"""

_IELTS_VISION = """
Parse the IELTS exam from these images. Output a JSON array - one object per question.

Required fields: section_title, passage_text (first Q of section only), group_instruction,
word_limit, global_number, question_text, question_type, answer, choices_json, items_json, points.

Types: true_false_not_given | yes_no_not_given | matching | matching_headings |
       multiple_choice | checkbox | fill_blank | essay

JSON array:"""


# ══════════════════════════════════════════════════════════════════════════════
# FAMILY: GENERIC (fallback for rare subjects)
# ══════════════════════════════════════════════════════════════════════════════

_GENERIC_SYSTEM = r"""You are a Vietnamese K12 exam parser expert. Extract ALL problems from documents into structured JSON.

SUBJECT DETECTION:
- Detect subject from document header, content, and question style
- Subject codes: toan, ngu-van, tieng-anh, khtn, vat-li, hoa-hoc, sinh-hoc, lich-su, dia-li, gdcd, gdktpl, tin-hoc, cong-nghe, tieng-viet, khoa-hoc, ls-dl, dao-duc, am-nhac, my-thuat, gdtc, hdtn, gdqpan

GENERAL RULES:
- Copy ALL content EXACTLY as written — text, numbers, formulas, names
- Images: nếu markdown input có ![alt](/media/...) hoặc ![alt](url) → COPY NGUYÊN. Chỉ fallback [HÌNH VẼ] với mô tả khi KHÔNG có image link sẵn.
- Tables → [BẢNG DỮ LIỆU]
- Preserve Vietnamese diacritics exactly

TYPE: TN | TL | Thực hành | Bài tập | Giải thích | Nhận xét

""" + _LATEX_RULES + "\n\n" + _COMMON_EXTRACTION + "\n\n" + _COMMON_DIFFICULTY + "\n\n" + _COMMON_JSON_SCHEMA + "\n\n" + _COMMON_SPECIAL_CASES + "\n\n" + _COMMON_OUTPUT

_GENERIC_PARSE_V1 = """Extract ALL questions from this document into a JSON array.
RULES: Close all JSON properly. Copy all content EXACTLY (numbers, formulas, text).
If text contains SOLUTIONS below questions, extract them into solution_steps.

{text}

JSON array:"""

_GENERIC_PARSE_V2 = """Extract questions to JSON array. Copy every detail exactly. Include solution_steps if present.

{text}

JSON:"""

_GENERIC_VISION = r"""Extract ALL questions from these page images into a JSON array.

CRITICAL RULES:
- Scan EVERY page. Missing questions is unacceptable.
- Copy ALL content EXACTLY — text, formulas, diagrams descriptions.
- Questions with SOLUTIONS: extract solution_steps too.
- Multi-part (a,b,c) → ONE object, steps prefixed "--- a)", "--- b)"
- If answers at end of doc, match by CONTENT not number.
- Output ONLY raw JSON array — no markdown.

JSON array:"""


# ── Build prompt config registry ─────────────────────────────────────────────

PROMPT_CONFIGS: dict[str, PromptConfig] = {
    "ielts": PromptConfig(
        family="ielts",
        system_prompt=_IELTS_SYSTEM,
        parse_prompt_v1=_IELTS_PARSE_V1,
        parse_prompt_v2=_IELTS_PARSE_V2,
        parse_prompt_v3=_SHARED_PARSE_V3,
    ),
    "math": PromptConfig(
        family="math",
        system_prompt=_MATH_SYSTEM,
        parse_prompt_v1=_MATH_PARSE_V1,
        parse_prompt_v2=_MATH_PARSE_V2,
        parse_prompt_v3=_SHARED_PARSE_V3,
    ),
    "physics": PromptConfig(
        family="physics",
        system_prompt=_PHYSICS_SYSTEM,
        parse_prompt_v1=_PHYSICS_PARSE_V1,
        parse_prompt_v2=_PHYSICS_PARSE_V2,
        parse_prompt_v3=_SHARED_PARSE_V3,
    ),
    "chemistry": PromptConfig(
        family="chemistry",
        system_prompt=_CHEMISTRY_SYSTEM,
        parse_prompt_v1=_CHEMISTRY_PARSE_V1,
        parse_prompt_v2=_CHEMISTRY_PARSE_V2,
        parse_prompt_v3=_SHARED_PARSE_V3,
    ),
    "biology": PromptConfig(
        family="biology",
        system_prompt=_BIOLOGY_SYSTEM,
        parse_prompt_v1=_BIOLOGY_PARSE_V1,
        parse_prompt_v2=_BIOLOGY_PARSE_V2,
        parse_prompt_v3=_SHARED_PARSE_V3,
    ),
    "khtn": PromptConfig(
        family="khtn",
        system_prompt=_KHTN_SYSTEM,
        parse_prompt_v1=_KHTN_PARSE_V1,
        parse_prompt_v2=_KHTN_PARSE_V2,
        parse_prompt_v3=_SHARED_PARSE_V3,
    ),
    "literature": PromptConfig(
        family="literature",
        system_prompt=_LITERATURE_SYSTEM,
        parse_prompt_v1=_LITERATURE_PARSE_V1,
        parse_prompt_v2=_LITERATURE_PARSE_V2,
        parse_prompt_v3=_SHARED_PARSE_V3,
    ),
    "english": PromptConfig(
        family="english",
        system_prompt=_ENGLISH_SYSTEM,
        parse_prompt_v1=_ENGLISH_PARSE_V1,
        parse_prompt_v2=_ENGLISH_PARSE_V2,
        parse_prompt_v3=_SHARED_PARSE_V3,
    ),
    "social": PromptConfig(
        family="social",
        system_prompt=_SOCIAL_SYSTEM,
        parse_prompt_v1=_SOCIAL_PARSE_V1,
        parse_prompt_v2=_SOCIAL_PARSE_V2,
        parse_prompt_v3=_SHARED_PARSE_V3,
    ),
    "informatics": PromptConfig(
        family="informatics",
        system_prompt=_INFORMATICS_SYSTEM,
        parse_prompt_v1=_INFORMATICS_PARSE_V1,
        parse_prompt_v2=_INFORMATICS_PARSE_V2,
        parse_prompt_v3=_SHARED_PARSE_V3,
    ),
    "generic": PromptConfig(
        family="generic",
        system_prompt=_GENERIC_SYSTEM,
        parse_prompt_v1=_GENERIC_PARSE_V1,
        parse_prompt_v2=_GENERIC_PARSE_V2,
        parse_prompt_v3=_SHARED_PARSE_V3,
    ),
}


def get_prompt_config(subject_code: Optional[str] = None) -> PromptConfig:
    """Get the prompt configuration for a subject code.

    Falls back to 'generic' if subject_code is unknown or None.
    """
    if not subject_code:
        return PROMPT_CONFIGS["generic"]
    family = SUBJECT_TO_FAMILY.get(subject_code, "generic")
    return PROMPT_CONFIGS[family]
