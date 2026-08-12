from __future__ import annotations

import re
from dataclasses import dataclass


_MATH_BLOCK_RE = re.compile(r"\$\$([\s\S]*?)\$\$")
_MATH_INLINE_RE = re.compile(r"(?<!\$)\$([^$\n]{1,1000})\$(?!\$)")
_LATEX_COMMAND_RE = re.compile(
    r"\\(?:frac|sqrt|int|sum|lim|prod|left|right|begin|end|alpha|beta|gamma|"
    r"theta|pi|sin|cos|tan|log|ln|cdot|times|leq|geq|neq|infty)(?![A-Za-z])"
)


@dataclass
class LatexAnalysis:
    formula_count: int
    valid_count: int
    invalid_count: int
    valid_ratio: float
    issues: list[str]


def extract_math_expressions(text: str) -> list[str]:
    raw = text or ""
    expressions = [m.group(1) for m in _MATH_BLOCK_RE.finditer(raw)]
    without_blocks = _MATH_BLOCK_RE.sub(" ", raw)
    expressions.extend(m.group(1) for m in _MATH_INLINE_RE.finditer(without_blocks))
    return expressions


def count_latex_commands(text: str) -> int:
    return len(_LATEX_COMMAND_RE.findall(text or ""))


def analyze_latex(text: str) -> LatexAnalysis:
    raw = text or ""
    expressions = extract_math_expressions(raw)
    command_count = count_latex_commands(raw)
    issues: list[str] = []

    if _has_unmatched_dollar(raw):
        issues.append("unmatched_dollar")

    valid = 0
    invalid = 0
    for expr in expressions:
        expr_issues = validate_latex_expression(expr)
        if expr_issues:
            invalid += 1
            issues.extend(expr_issues)
        else:
            valid += 1

    if not expressions and command_count:
        expr_issues = validate_latex_expression(raw)
        if expr_issues:
            invalid += 1
            issues.extend(expr_issues)
        else:
            valid += 1

    if _has_unmatched_dollar(raw) and not invalid:
        invalid += 1

    formula_count = max(len(expressions), valid + invalid, command_count)
    if formula_count and valid + invalid < formula_count:
        valid += formula_count - (valid + invalid)
    valid_ratio = valid / formula_count if formula_count else 1.0
    return LatexAnalysis(
        formula_count=formula_count,
        valid_count=valid,
        invalid_count=invalid,
        valid_ratio=round(valid_ratio, 3),
        issues=sorted(set(issues)),
    )


def validate_latex_expression(expr: str) -> list[str]:
    issues: list[str] = []
    if not _balanced_braces(expr):
        issues.append("unbalanced_braces")
    if len(re.findall(r"\\left(?![A-Za-z])", expr)) != len(re.findall(r"\\right(?![A-Za-z])", expr)):
        issues.append("left_right_mismatch")
    if _count_frac_commands(expr) > _count_valid_frac_commands(expr):
        issues.append("frac_missing_args")
    if re.search(r"\\sqrt(?!\s*(?:\[[^\]]*\])?\s*\{)", expr):
        issues.append("sqrt_missing_brace")
    return issues


def _has_unmatched_dollar(text: str) -> bool:
    stripped = re.sub(r"\\\$", "", text or "")
    return stripped.count("$") % 2 == 1


def _balanced_braces(text: str) -> bool:
    depth = 0
    escaped = False
    for char in text or "":
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _count_frac_commands(expr: str) -> int:
    return len(re.findall(r"\\frac(?![A-Za-z])", expr or ""))


def _count_valid_frac_commands(expr: str) -> int:
    return len(re.findall(r"\\frac\s*\{[^{}]*\}\s*\{[^{}]*\}", expr or ""))
