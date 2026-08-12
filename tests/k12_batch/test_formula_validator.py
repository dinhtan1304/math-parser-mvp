"""Unit tests for ``app.services.k12_batch.formula_validator``."""

from __future__ import annotations

import pytest

from app.services.k12_batch.formula_validator import (
    FormulaRetryStats,
    find_invalid_formulas,
    revalidate_markdown,
    validate_latex,
)


class TestValidateLatex:
    @pytest.mark.parametrize(
        "latex",
        [
            r"\frac{1}{2}",
            r"x^2 + y^2 = z^2",
            r"\int_0^1 x \, dx",
            r"\sum_{i=1}^n i",
            r"\sqrt{a^2 + b^2}",
            r"\dfrac{1}{2}",
            r"x_{i+1}",
            r"[a, b]",
            r"\binom{n}{k}",
        ],
    )
    def test_valid_latex(self, latex: str) -> None:
        ok, err = validate_latex(latex)
        assert ok, f"expected valid: {latex!r} got err={err}"

    @pytest.mark.parametrize(
        ("latex", "expected_marker"),
        [
            (r"\frac{1{2}}", "missing_arg2"),
            (r"\frac{a}", "missing_arg2"),
            (r"\dfrac{1}{2", "unbalanced_curly"),
            (r"\sqrt{1 + ", "trailing_operator"),
            (r"a + b =", "trailing_operator"),
            (r"\frac", "missing_arg1"),
            (r"[a, b", "unbalanced_square"),
            (r"x \\", "trailing_backslash"),
            ("", "empty"),
        ],
    )
    def test_invalid_latex(self, latex: str, expected_marker: str) -> None:
        ok, err = validate_latex(latex)
        assert not ok, f"expected invalid: {latex!r}"
        assert err and expected_marker in err, f"unexpected error label {err!r} for {latex!r}"


class TestFindInvalidFormulas:
    def test_finds_block_and_inline(self) -> None:
        md = (
            "Câu 1. Tính $$\\frac{1}{2}$$ và $$\\frac{1{2}}$$.\n"
            "Câu 2. Cho $a + b =$ một số.\n"
            "Câu 3. Tính $$\\sqrt{1 + $$.\n"
        )
        issues = find_invalid_formulas(md)
        kinds = {i.kind for i in issues}
        assert kinds == {"block", "inline"}
        assert len(issues) == 3

    def test_short_inline_skipped(self) -> None:
        # `$x$` is too short to flag — those are common variable names.
        md = "Câu 1. Cho $x$ và $y$."
        assert find_invalid_formulas(md) == []

    def test_clean_markdown_has_no_issues(self) -> None:
        md = "Câu 1. Tính $$\\frac{a}{b}$$ với $a, b > 0$."
        assert find_invalid_formulas(md) == []


class TestRevalidateMarkdown:
    def test_no_invalid_returns_input_untouched(self, tmp_path) -> None:
        md = "Câu 1. Tính $$\\frac{1}{2}$$."
        new_md, stats = revalidate_markdown(md, [], tmp_path / "fake.pdf", tmp_path)
        assert new_md == md
        assert stats.invalid == 0
        assert stats.retried == 0

    def test_no_bbox_logged_when_invalid_but_no_content_list(self, tmp_path) -> None:
        md = "Câu 1. Tính $$\\frac{1{2}}$$."
        new_md, stats = revalidate_markdown(md, [], tmp_path / "fake.pdf", tmp_path)
        # We can't retry without bbox info — verify the stat is recorded.
        assert stats.invalid == 1
        assert stats.no_bbox == 1
        assert stats.retried == 0
        # Markdown unchanged (no recovery).
        assert new_md == md

    def test_stats_dataclass_to_dict(self) -> None:
        stats = FormulaRetryStats(scanned=10, invalid=2, retried=2, recovered=1)
        d = stats.to_dict()
        assert d["scanned"] == 10 and d["recovered"] == 1
