"""
LaTeX → OMML (Office Math Markup Language) converter.

Converts LaTeX math expressions to OMML XML elements that can be
inserted into python-docx paragraphs for native Word equation rendering.

Supported patterns (covers ~95% of Vietnamese math exam content):
  - \\frac{a}{b}         → Fraction
  - \\sqrt{x}, \\sqrt[n]{x} → Radical
  - x^{n}, x^2           → Superscript
  - x_{n}, x_1           → Subscript
  - \\left( \\right)       → Delimiters
  - \\ge, \\le, \\ne, etc.  → Symbols
  - Greek letters          → Unicode
  - \\begin{cases}         → (rendered as text fallback)
  - Nested expressions     → Full recursion

Usage:
    from app.services.latex_to_omml import add_math_to_paragraph

    p = doc.add_paragraph()
    add_math_to_paragraph(p, "Cho biểu thức $B = \\frac{a}{b}$ với $a > 0$.")
"""

import re
from lxml import etree
from docx.oxml.ns import qn

# ─── Namespace ────────────────────────────────────────────────
M = 'http://schemas.openxmlformats.org/officeDocument/2006/math'
W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


# ─── OMML XML builders ───────────────────────────────────────

def _m(tag):
    """Create an OMML element: _m('f') → <m:f/>"""
    return etree.Element(qn(f'm:{tag}'))


def _m_sub(parent, tag):
    """Append an OMML child: _m_sub(frac, 'num') → <m:num/> inside frac."""
    return etree.SubElement(parent, qn(f'm:{tag}'))


def _m_run(text: str, italic: bool = True):
    """Create <m:r><m:rPr>...</m:rPr><m:t>text</m:t></m:r>"""
    r = _m('r')
    if not italic:
        rpr = _m_sub(r, 'rPr')
        sty = _m_sub(rpr, 'sty')
        sty.set(qn('m:val'), 'p')  # 'p' = plain (not italic)
    t = _m_sub(r, 't')
    t.text = text or ''
    t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    return r


def _w_run(parent, text: str, bold=False, italic=False, size=None, color=None):
    """Create a regular Word text run <w:r>."""
    r = etree.SubElement(parent, qn('w:r'))
    if bold or italic or size or color:
        rpr = etree.SubElement(r, qn('w:rPr'))
        if bold:
            etree.SubElement(rpr, qn('w:b'))
        if italic:
            etree.SubElement(rpr, qn('w:i'))
        if size:
            sz = etree.SubElement(rpr, qn('w:sz'))
            sz.set(qn('w:val'), str(size))
        if color:
            c = etree.SubElement(rpr, qn('w:color'))
            c.set(qn('w:val'), color)
    t = etree.SubElement(r, qn('w:t'))
    t.text = text
    t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
    return r


# ─── Symbol maps ─────────────────────────────────────────────

LATEX_SYMBOLS = {
    # Comparison
    '\\ge': '≥', '\\geq': '≥', '\\le': '≤', '\\leq': '≤',
    '\\ne': '≠', '\\neq': '≠', '\\approx': '≈',
    # Operators
    '\\pm': '±', '\\mp': '∓', '\\times': '×', '\\cdot': '·',
    '\\div': '÷', '\\circ': '∘',
    # Arrows
    '\\to': '→', '\\rightarrow': '→', '\\leftarrow': '←',
    '\\Rightarrow': '⇒', '\\Leftrightarrow': '⇔',
    # Sets
    '\\in': '∈', '\\notin': '∉', '\\subset': '⊂',
    '\\cup': '∪', '\\cap': '∩', '\\emptyset': '∅',
    # Greek lowercase
    '\\alpha': 'α', '\\beta': 'β', '\\gamma': 'γ', '\\delta': 'δ',
    '\\epsilon': 'ε', '\\varepsilon': 'ε', '\\zeta': 'ζ',
    '\\eta': 'η', '\\theta': 'θ', '\\vartheta': 'ϑ',
    '\\iota': 'ι', '\\kappa': 'κ', '\\lambda': 'λ',
    '\\mu': 'μ', '\\nu': 'ν', '\\xi': 'ξ',
    '\\pi': 'π', '\\rho': 'ρ', '\\sigma': 'σ',
    '\\tau': 'τ', '\\upsilon': 'υ', '\\phi': 'φ', '\\varphi': 'φ',
    '\\chi': 'χ', '\\psi': 'ψ', '\\omega': 'ω',
    # Greek uppercase
    '\\Gamma': 'Γ', '\\Delta': 'Δ', '\\Theta': 'Θ',
    '\\Lambda': 'Λ', '\\Xi': 'Ξ', '\\Pi': 'Π',
    '\\Sigma': 'Σ', '\\Phi': 'Φ', '\\Psi': 'Ψ', '\\Omega': 'Ω',
    # Misc
    '\\infty': '∞', '\\partial': '∂', '\\nabla': '∇',
    '\\forall': '∀', '\\exists': '∃',
    '\\dots': '…', '\\cdots': '⋯', '\\ldots': '…',
    '\\quad': ' ', '\\qquad': '  ', '\\,': ' ',
    '\\;': ' ', '\\:': ' ', '\\!': '',
    # Spacing/formatting
    '\\left': '', '\\right': '', '\\Big': '', '\\big': '',
    '\\Bigg': '', '\\bigg': '',
    '\\text': '',  # handled separately
    '\\mathrm': '', '\\mathbf': '', '\\mathit': '',
}

# Characters that map to delimiters in OMML
DELIMITERS = {
    '(': '(', ')': ')',
    '[': '[', ']': ']',
    '\\{': '{', '\\}': '}',
    '|': '|', '.': '',  # \right. = invisible
}


# ═══════════════════════════════════════════════════════════════
#  LATEX TOKENIZER
# ═══════════════════════════════════════════════════════════════

def _tokenize(latex: str) -> list:
    """
    Tokenize LaTeX string into a list of tokens.
    Token types: 'cmd', 'text', 'group', 'sup', 'sub', 'open', 'close'
    """
    tokens = []
    i = 0
    n = len(latex)

    while i < n:
        c = latex[i]

        if c == '\\':
            # Read command
            j = i + 1
            if j < n and not latex[j].isalpha():
                # Single-char command: \{, \}, \,, etc.
                tokens.append(('cmd', '\\' + latex[j]))
                i = j + 1
            else:
                while j < n and latex[j].isalpha():
                    j += 1
                tokens.append(('cmd', latex[i:j]))
                i = j

        elif c == '{':
            # Read group content
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if latex[j] == '{' and (j == 0 or latex[j-1] != '\\'):
                    depth += 1
                elif latex[j] == '}' and (j == 0 or latex[j-1] != '\\'):
                    depth -= 1
                j += 1
            tokens.append(('group', latex[i+1:j-1]))
            i = j

        elif c == '^':
            tokens.append(('sup',))
            i += 1

        elif c == '_':
            tokens.append(('sub',))
            i += 1

        elif c in ' \t':
            # Accumulate whitespace
            i += 1

        elif c == '\n':
            i += 1

        else:
            # Regular character(s)
            tokens.append(('text', c))
            i += 1

    return tokens


# ═══════════════════════════════════════════════════════════════
#  LATEX → OMML PARSER (recursive descent)
# ═══════════════════════════════════════════════════════════════

class LaTeXToOMML:
    """Convert tokenized LaTeX to OMML XML elements."""

    def __init__(self):
        self.tokens = []
        self.pos = 0

    def convert(self, latex: str) -> list:
        """
        Convert LaTeX math string to list of OMML elements.
        Returns list of lxml Elements (m:r, m:f, m:rad, m:sSup, etc.)
        """
        self.tokens = _tokenize(latex.strip())
        self.pos = 0
        return self._parse_expr()

    def _peek(self):
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def _advance(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _parse_expr(self) -> list:
        """Parse a sequence of elements."""
        elements = []
        while self.pos < len(self.tokens):
            tok = self._peek()
            if tok is None:
                break

            if tok[0] == 'cmd':
                self._advance()
                cmd = tok[1]
                elem = self._handle_command(cmd)
                if elem is not None:
                    elements.append(elem)

            elif tok[0] == 'group':
                self._advance()
                sub_elements = LaTeXToOMML().convert(tok[1])
                elements.extend(sub_elements)

            elif tok[0] == 'sup':
                self._advance()
                # Get the superscript content
                sup_content = self._read_next_arg()
                # Attach to previous element
                base = elements.pop() if elements else _m_run('')
                elements.append(self._make_sup(base, sup_content))

            elif tok[0] == 'sub':
                self._advance()
                sub_content = self._read_next_arg()
                base = elements.pop() if elements else _m_run('')
                # Check if followed by ^ (subscript + superscript)
                if self._peek() and self._peek()[0] == 'sup':
                    self._advance()
                    sup_content = self._read_next_arg()
                    elements.append(self._make_subsup(base, sub_content, sup_content))
                else:
                    elements.append(self._make_sub(base, sub_content))

            elif tok[0] == 'text':
                self._advance()
                elements.append(_m_run(tok[1]))

            else:
                self._advance()

        return elements

    def _read_next_arg(self) -> list:
        """Read the next argument (group or single token) and convert to OMML."""
        tok = self._peek()
        if tok is None:
            return [_m_run('')]

        if tok[0] == 'group':
            self._advance()
            return LaTeXToOMML().convert(tok[1])
        else:
            # Single character/command
            self._advance()
            if tok[0] == 'cmd':
                elem = self._handle_command(tok[1])
                return [elem] if elem is not None else [_m_run('')]
            elif tok[0] == 'text':
                return [_m_run(tok[1])]
            else:
                return [_m_run('')]

    def _handle_command(self, cmd: str):
        """Handle a LaTeX command and return an OMML element."""

        # ── Fraction ──
        if cmd == '\\frac':
            num_elems = self._read_next_arg()
            den_elems = self._read_next_arg()
            return self._make_frac(num_elems, den_elems)

        # ── Square root / nth root ──
        elif cmd == '\\sqrt':
            # Check for optional [n]
            degree = None
            if self._peek() and self._peek()[0] == 'text' and self._peek()[1] == '[':
                # Read until ]
                self._advance()  # skip [
                deg_text = ''
                while self._peek() and not (self._peek()[0] == 'text' and self._peek()[1] == ']'):
                    tok = self._advance()
                    if tok[0] == 'text':
                        deg_text += tok[1]
                    elif tok[0] == 'group':
                        deg_text += tok[1]
                if self._peek():
                    self._advance()  # skip ]
                degree = deg_text
            content = self._read_next_arg()
            return self._make_radical(content, degree)

        # ── Limits ──
        elif cmd == '\\lim':
            r = _m_run('lim', italic=False)
            return r

        # ── Trig functions ──
        elif cmd in ('\\sin', '\\cos', '\\tan', '\\cot', '\\sec', '\\csc',
                      '\\arcsin', '\\arccos', '\\arctan',
                      '\\log', '\\ln', '\\exp', '\\min', '\\max'):
            func_name = cmd[1:]  # strip backslash
            return _m_run(func_name, italic=False)

        # ── Delimiters ──
        elif cmd in ('\\left', '\\right'):
            # Read the delimiter character
            tok = self._peek()
            delim = ''
            if tok:
                self._advance()
                if tok[0] == 'text':
                    delim = tok[1]
                elif tok[0] == 'cmd':
                    delim = DELIMITERS.get(tok[1], tok[1].lstrip('\\'))
            if delim == '.':
                return None  # invisible delimiter
            return _m_run(delim)

        # ── Begin/End environments ──
        elif cmd == '\\begin':
            env = self._read_next_arg()
            # Skip environment content — render as text fallback
            return _m_run('{', italic=False)

        elif cmd == '\\end':
            self._read_next_arg()
            return _m_run('}', italic=False)

        # ── Text mode ──
        elif cmd in ('\\text', '\\mathrm', '\\textrm', '\\operatorname'):
            text_content = self._read_next_arg()
            # Flatten to plain text
            texts = []
            for el in text_content:
                t_el = el.find(qn('m:t'))
                if t_el is not None and t_el.text:
                    texts.append(t_el.text)
            return _m_run(''.join(texts), italic=False)

        elif cmd == '\\mathbf':
            return self._read_next_arg()[0] if self._read_next_arg() else _m_run('')

        # ── Symbol lookup ──
        elif cmd in LATEX_SYMBOLS:
            symbol = LATEX_SYMBOLS[cmd]
            if symbol:
                return _m_run(symbol)
            return None

        # ── Unknown command → render as text ──
        else:
            # Strip backslash, show as-is
            return _m_run(cmd.lstrip('\\'), italic=False)

    # ─── OMML structure builders ──────────────────────────────

    def _make_frac(self, num_elems: list, den_elems: list):
        """Build <m:f> fraction element."""
        f = _m('f')
        fpr = _m_sub(f, 'fPr')
        # Default bar fraction (type not needed, bar is default)

        num = _m_sub(f, 'num')
        for el in num_elems:
            num.append(el)

        den = _m_sub(f, 'den')
        for el in den_elems:
            den.append(el)

        return f

    def _make_radical(self, content: list, degree=None):
        """Build <m:rad> radical element."""
        rad = _m('rad')
        radpr = _m_sub(rad, 'radPr')
        if degree is None:
            # Hide degree for sqrt
            dh = _m_sub(radpr, 'degHide')
            dh.set(qn('m:val'), '1')

        deg = _m_sub(rad, 'deg')
        if degree:
            deg.append(_m_run(degree))

        e = _m_sub(rad, 'e')
        for el in content:
            e.append(el)

        return rad

    def _make_sup(self, base, sup_content: list):
        """Build <m:sSup> superscript element."""
        ssup = _m('sSup')
        e = _m_sub(ssup, 'e')
        e.append(base)
        sup = _m_sub(ssup, 'sup')
        for el in sup_content:
            sup.append(el)
        return ssup

    def _make_sub(self, base, sub_content: list):
        """Build <m:sSub> subscript element."""
        ssub = _m('sSub')
        e = _m_sub(ssub, 'e')
        e.append(base)
        sub = _m_sub(ssub, 'sub')
        for el in sub_content:
            sub.append(el)
        return ssub

    def _make_subsup(self, base, sub_content: list, sup_content: list):
        """Build <m:sSubSup> combined sub+superscript."""
        sss = _m('sSubSup')
        e = _m_sub(sss, 'e')
        e.append(base)
        sub = _m_sub(sss, 'sub')
        for el in sub_content:
            sub.append(el)
        sup = _m_sub(sss, 'sup')
        for el in sup_content:
            sup.append(el)
        return sss


# ═══════════════════════════════════════════════════════════════
#  PUBLIC API: Add math-rich text to python-docx paragraph
# ═══════════════════════════════════════════════════════════════

# Regex to find math regions: $...$ or $$...$$
_MATH_PATTERN = re.compile(
    r'(\$\$.*?\$\$|\$(?!\$).*?\$)',
    re.DOTALL,
)


def add_math_to_paragraph(para, text: str, font_size=None, font_color=None, bold=False):
    """
    Add text with LaTeX math to a python-docx paragraph.
    
    Non-math text → regular Word runs
    $...$ math → OMML equation objects
    
    Args:
        para: python-docx Paragraph object
        text: string potentially containing $...$ LaTeX math
        font_size: Pt size for non-math text (e.g. 12)
        font_color: hex color string for non-math text (e.g. "008050")
        bold: bold non-math text
    """
    if not text:
        return

    # Split text into math and non-math segments
    segments = _split_math(text)

    for is_math, content in segments:
        if is_math:
            # Convert LaTeX → OMML and insert
            _insert_omml(para, content)
        else:
            # Regular text run
            if content:
                _w_run(
                    para._element, content,
                    bold=bold,
                    size=font_size,
                    color=font_color,
                )


def _split_math(text: str) -> list:
    """
    Split text into segments of (is_math, content).
    
    "Cho $x > 0$ thì" → [(False, "Cho "), (True, "x > 0"), (False, " thì")]
    """
    segments = []
    last = 0

    for m in _MATH_PATTERN.finditer(text):
        # Non-math before this match
        if m.start() > last:
            segments.append((False, text[last:m.start()]))

        # Math content (strip $ delimiters)
        math_str = m.group()
        if math_str.startswith('$$') and math_str.endswith('$$'):
            math_str = math_str[2:-2].strip()
        elif math_str.startswith('$') and math_str.endswith('$'):
            math_str = math_str[1:-1].strip()

        segments.append((True, math_str))
        last = m.end()

    # Remaining non-math text
    if last < len(text):
        segments.append((False, text[last:]))

    # If no math found, return as plain text
    if not segments:
        segments.append((False, text))

    return segments


def _insert_omml(para, latex: str):
    """Convert LaTeX to OMML and insert into paragraph element."""
    try:
        converter = LaTeXToOMML()
        elements = converter.convert(latex)

        if not elements:
            # Fallback: insert as plain text
            _w_run(para._element, latex)
            return

        # Create <m:oMath> container
        omath = etree.SubElement(para._element, qn('m:oMath'))
        for elem in elements:
            omath.append(elem)

    except Exception:
        # Fallback: insert raw text if conversion fails
        _w_run(para._element, latex)


def latex_to_text(text: str) -> str:
    """
    Fallback: Convert LaTeX to readable Unicode text.
    Used when OMML is not appropriate (e.g., plain text contexts).
    
    "$\\frac{a}{b}$" → "(a)/(b)"
    """
    if not text:
        return ""

    def _convert_math(match):
        s = match.group()
        if s.startswith('$$'):
            s = s[2:-2]
        else:
            s = s[1:-1]
        return _latex_math_to_unicode(s)

    return _MATH_PATTERN.sub(_convert_math, text)


def _latex_math_to_unicode(s: str) -> str:
    """Convert LaTeX math content to Unicode text."""
    # Fractions
    s = re.sub(r'\\frac\{([^{}]+)\}\{([^{}]+)\}', r'(\1)/(\2)', s)
    # Nested fractions (2nd pass)
    s = re.sub(r'\\frac\{([^{}]+)\}\{([^{}]+)\}', r'(\1)/(\2)', s)

    # Square root
    s = re.sub(r'\\sqrt\[(\d+)\]\{([^{}]+)\}', r'ⁿ√(\2)', s)
    s = re.sub(r'\\sqrt\{([^{}]+)\}', r'√(\1)', s)

    # Super/subscripts (simple single char)
    sup_map = str.maketrans('0123456789+-=()niab', '⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱᵃᵇ')
    sub_map = str.maketrans('0123456789+-=()aeioux', '₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑᵢₒᵤₓ')

    def _sup_repl(m):
        t = m.group(1)
        return t.translate(sup_map)

    def _sub_repl(m):
        t = m.group(1)
        return t.translate(sub_map)

    s = re.sub(r'\^\{([^{}]+)\}', _sup_repl, s)
    s = re.sub(r'\^(\w)', _sup_repl, s)
    s = re.sub(r'_\{([^{}]+)\}', _sub_repl, s)
    s = re.sub(r'_(\w)', _sub_repl, s)

    # Symbols
    for cmd, sym in LATEX_SYMBOLS.items():
        if sym:
            s = s.replace(cmd, sym)

    # Clean remaining backslash commands
    s = re.sub(r'\\(left|right|Big|big|Bigg|bigg)\s*', '', s)
    s = re.sub(r'\\(begin|end)\{[^}]*\}', '', s)
    s = re.sub(r'\\([a-zA-Z]+)', lambda m: m.group(1), s)

    # Clean up
    s = re.sub(r'\s+', ' ', s).strip()
    return s