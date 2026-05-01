"""Single source of truth for LLM token pricing (USD per 1M tokens).

Used by both the batch script (`scripts/process_batch.py`) to build
`cost_report.json`, and the dashboard (`src/dashboard/app.py`) to compute
filtered cost metrics on the fly. Keeping it here means the two views can
never drift in price assumptions.

Update whenever providers change pricing pages:
- Gemini:    https://ai.google.dev/pricing
- OpenAI:    https://openai.com/api/pricing/
- Anthropic: https://www.anthropic.com/pricing
"""

from __future__ import annotations

from typing import Dict, Optional

PRICING_USD_PER_1M: Dict[str, Dict[str, float]] = {
    "gemini-2.5-flash-lite":          {"in": 0.10, "out": 0.40},
    "gemini-3.1-flash-lite-preview":  {"in": 0.10, "out": 0.40},
    "gpt-4.1-nano":                   {"in": 0.10, "out": 0.40},
    "gpt-5.4-nano":                   {"in": 0.20, "out": 1.25},
    "gpt-5-mini":                     {"in": 0.25, "out": 2.00},
    "claude-haiku-4-5":               {"in": 1.00, "out": 5.00},
    "claude-haiku-4-5-20251001":      {"in": 1.00, "out": 5.00},
}


def price_for(model: Optional[str]) -> Dict[str, float]:
    """Return {"in": $/Mtok, "out": $/Mtok} for the given model.

    Unknown / None models map to zero pricing. The caller is responsible for
    deciding what that means (we use it as "don't add to cost", which is safer
    than guessing the wrong model).
    """
    if not model:
        return {"in": 0.0, "out": 0.0}
    return PRICING_USD_PER_1M.get(model, {"in": 0.0, "out": 0.0})


def call_cost_usd(tokens_in: int, tokens_out: int, model: Optional[str]) -> float:
    """Cost in USD for a single LLM call given its tokens and model."""
    p = price_for(model)
    return (tokens_in / 1_000_000) * p["in"] + (tokens_out / 1_000_000) * p["out"]
