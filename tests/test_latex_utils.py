from app.benchmark.latex_utils import analyze_latex


def test_analyze_latex_counts_valid_inline_block_and_commands():
    text = "Tính $x=\\frac{1}{2}$ và $$\\sqrt{x}+\\sum_{i=1}^{n} i$$."

    result = analyze_latex(text)

    assert result.formula_count >= 2
    assert result.valid_count == result.formula_count
    assert result.invalid_count == 0
    assert result.valid_ratio == 1.0


def test_analyze_latex_detects_unmatched_dollar():
    result = analyze_latex("Câu 1. Tính $x+1.")

    assert result.invalid_count >= 1
    assert result.valid_ratio < 1.0
    assert any("unmatched_dollar" in issue for issue in result.issues)


def test_analyze_latex_detects_unbalanced_braces():
    result = analyze_latex("Câu 1. $x=\\frac{1}{2$")

    assert result.invalid_count >= 1
    assert any("unbalanced_braces" in issue for issue in result.issues)


def test_analyze_latex_detects_frac_and_sqrt_missing_arguments():
    result = analyze_latex("Câu 1. $\\frac{1} + \\sqrt x$")

    assert result.invalid_count >= 1
    assert any("frac_missing_args" in issue for issue in result.issues)
    assert any("sqrt_missing_brace" in issue for issue in result.issues)


def test_analyze_latex_detects_left_without_right():
    result = analyze_latex("Câu 1. $\\left( x+1$")

    assert result.invalid_count >= 1
    assert any("left_right_mismatch" in issue for issue in result.issues)
