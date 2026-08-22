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

from lo_core.errors import ValidationError

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
    # OpenAI, via the openai-compatible provider.
    #
    # These rates describe **OpenAI's own endpoint only**. The same model name
    # served by Groq, Together or OpenRouter costs something different, and a
    # self-hosted vLLM server costs nothing per token at all — so the provider
    # only consults this table when `base_url` points at api.openai.com, and
    # records None otherwise. See `_should_price` in openai_provider.py.
    "gpt-4.1": (Decimal("2.00"), Decimal("8.00")),
    "gpt-4.1-mini": (Decimal("0.40"), Decimal("1.60")),
    "gpt-4.1-nano": (Decimal("0.10"), Decimal("0.40")),
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "o3-mini": (Decimal("1.10"), Decimal("4.40")),
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

# OpenAI's reasoning models reject sampling parameters the same way, but their
# names carry version suffixes (`o3-mini-2025-01-31`), so they are matched by
# prefix rather than by exact membership.
#
# Like the pricing above this is a hand-maintained snapshot, and it errs toward
# the families that are unambiguous today rather than guessing at ones that are
# not. A model missing from here fails at the provider with a readable 400
# instead of at request time — worse, but not wrong.
OPENAI_REASONING_PREFIXES: tuple[str, ...] = ("o1", "o3", "o4")


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


def rejects_sampling_parameters(model: str) -> bool:
    """Whether `model` refuses temperature/top_p/top_k outright."""
    if model in SAMPLING_UNSUPPORTED:
        return True
    return model.startswith(OPENAI_REASONING_PREFIXES)


def unsupported_sampling_parameters(model: str, parameters: dict[str, object]) -> list[str]:
    """Sampling parameters present in `parameters` that `model` will reject."""
    if not rejects_sampling_parameters(model):
        return []
    return sorted(SAMPLING_PARAMETERS & parameters.keys())


def assert_sampling_supported(model: str, parameters: dict[str, object]) -> None:
    """Raise if `model` will reject the sampling parameters in `parameters`.

    Called when an eval run is *requested*, so the failure is one 422 on one API
    call rather than N provider 400s discovered partway through a run that has
    already been paid for.

    Shared across providers rather than written per vendor: the situation is not
    Anthropic-specific. It arises anywhere a prompt version stores decoding
    parameters (ADR 0004) and the model named at run time is newer than the
    prompt — which is every reasoning model, from either vendor.
    """
    rejected = unsupported_sampling_parameters(model, parameters)
    if rejected:
        raise ValidationError(
            f"model {model!r} does not accept {', '.join(rejected)}. "
            "This prompt version records decoding parameters that reasoning models "
            "reject; remove them from the version's parameters, or run against a "
            "model that still accepts them."
        )
