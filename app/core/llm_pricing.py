"""Shared LLM (Gemini) pricing + token helpers.

Single source of truth for cost math used by the dashboard token-usage page,
the parse pipeline (writes ``cost_usd`` into ``ingest_stats``) and the per-user
daily token quota guard.
"""
import json

# Gemini 2.5 Flash pricing (USD per 1M tokens) — keep in sync with billing page.
GEMINI_INPUT_USD_PER_M = 0.30
GEMINI_OUTPUT_USD_PER_M = 2.50


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    return round(
        (input_tokens / 1_000_000) * GEMINI_INPUT_USD_PER_M
        + (output_tokens / 1_000_000) * GEMINI_OUTPUT_USD_PER_M,
        4,
    )


def extract_tokens_from_result_json(result_json: str | None) -> tuple[int, int, int]:
    """Return (input, output, calls) from an Exam.result_json ingest_stats blob."""
    if not result_json:
        return 0, 0, 0
    try:
        parsed = json.loads(result_json)
    except Exception:
        return 0, 0, 0
    if not isinstance(parsed, dict):
        return 0, 0, 0
    stats = parsed.get("ingest_stats") or {}
    if not isinstance(stats, dict):
        return 0, 0, 0
    try:
        inp = int(stats.get("estimated_input_tokens") or 0)
        out = int(stats.get("estimated_output_tokens") or 0)
        calls = int(stats.get("ai_text_calls") or 0)
    except (TypeError, ValueError):
        return 0, 0, 0
    return inp, out, calls
