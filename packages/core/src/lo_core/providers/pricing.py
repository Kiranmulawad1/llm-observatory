"""Model pricing and per-model parameter capabilities.

**This is a hand-maintained snapshot, and that is a deliberate tradeoff.** Cost
has to be computed at write time so `eval_results.cost_usd` is a durable record
of what a run actually cost — recomputing historical rows against today's rates
would silently rewrite history the first time a price changed. The cost of that
choice is this table going stale, so it carries the date it was last checked and
the API surfaces an explicit `pricing_stale` flag rather than quietly returning
wrong numbers.

`PRICING_CHECKED` must be bumped whenever the rates below are re-verified.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

PRICING_CHECKED = date(2026, 6, 24)

# USD per 1,000,000 tokens, as (input, output).
PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-fable-5": (Decimal("10.00"), Decimal("50.00")),
    "claude-mythos-5": (Decimal("10.00"), Decimal("50.00")),
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-8": (Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-7": (Decimal("5.00"), Decimal("25.00")),
    "claude-opus-4-6": (Decimal("5.00"), Decimal("25.00")),
    # Standard rate. An introductory discount was in effect at snapshot time;
    # deliberately not encoded, because a promotional rate with an expiry date
    # is exactly the kind of thing a static table gets wrong. Over-reporting
    # cost is the safer direction to be wrong in.
    "claude-sonnet-5": (Decimal("3.00"), Decimal("15.00")),
    "claude-sonnet-4-6": (Decimal("3.00"), Decimal("15.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
}

# Models that reject `temperature`, `top_p` and `top_k` outright — the request
# returns a 400 rather than ignoring the field.
#
# This matters here specifically because the prompt registry stores decoding
# parameters *with* the prompt version (ADR 0004), so a prompt authored against
# an older model carries a `temperature` that a newer model refuses. Catching it
# against this set turns a mid-run 400 into a validation error at run request.
SAMPLING_UNSUPPORTED: frozenset[str] = frozenset(
    {
        "claude-fable-5",
        "claude-mythos-5",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-sonnet-5",
    }
)

SAMPLING_PARAMETERS: frozenset[str] = frozenset({"temperature", "top_p", "top_k"})


def compute_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    """Cost in USD, or None when the model is not in the table.

    None rather than 0: an unpriced model is unknown cost, and recording it as
    zero would make a dashboard's total silently wrong instead of visibly
    incomplete.
    """
    rates = PRICING.get(model)
    if rates is None:
        return None
    input_rate, output_rate = rates
    million = Decimal(1_000_000)
    return (Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate) / million


def unsupported_sampling_parameters(model: str, parameters: dict[str, object]) -> list[str]:
    """Sampling parameters present in `parameters` that `model` will reject."""
    if model not in SAMPLING_UNSUPPORTED:
        return []
    return sorted(SAMPLING_PARAMETERS & parameters.keys())
