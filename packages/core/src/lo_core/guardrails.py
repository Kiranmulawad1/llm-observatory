"""Lightweight guardrail checks.

These run over *sampled production traffic*, which sets the design constraint:
they must be cheap enough to run continuously and deterministic enough that a
flag is explainable. Nothing here calls a model. Judge escalation exists, but it
is opt-in per project and runs only on what these already flagged.

Each check returns a severity in 0.0-1.0 plus the evidence, so the queue can
order by "worst first" and a reviewer can see *why* without re-deriving it.

**These are heuristics and will produce false positives.** That is the accepted
trade: a cheap check that flags too much and hands it to a human beats an
expensive check that runs on 1% of traffic. The control sample (see
GuardrailConfig.control_sample_rate) is what measures the other direction — the
failures these miss entirely.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from lo_core.logging import get_logger

log = get_logger(__name__)

# --- PII -------------------------------------------------------------------

# Deliberately narrow patterns. A loose email regex matches half of all URLs,
# and a check that fires constantly gets ignored — which is the same as not
# having it.
PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"),
    # 13-19 digits with optional separators, which covers the major card
    # networks. Validated with Luhn below rather than trusted on shape alone.
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # E.164 and common national formats.
    "phone": re.compile(r"\b(?:\+\d{1,3}[ -]?)?(?:\(\d{3}\)|\d{3})[ -]\d{3}[ -]\d{4}\b"),
    # Provider key prefixes. The highest-severity finding in this module: a
    # leaked key in a model's output is an active incident, not a quality issue.
    "api_key": re.compile(
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|lo_live_[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{36})\b"
    ),
    "aws_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
}

PII_SEVERITY: dict[str, float] = {
    "api_key": 1.0,
    "aws_key": 1.0,
    "credit_card": 0.9,
    "ssn": 0.9,
    "email": 0.4,
    "phone": 0.4,
}


def luhn_valid(digits: str) -> bool:
    """Luhn checksum, used to reject numbers that merely look like cards.

    Without it, any 16-digit order number or timestamp trips the credit-card
    check — and a check that cries wolf gets muted.
    """
    numbers = [int(d) for d in digits if d.isdigit()]
    if not 13 <= len(numbers) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(numbers)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


# --- Toxicity --------------------------------------------------------------

# A crude wordlist. Stated plainly: this catches slurs and overt abuse and
# nothing subtler — it cannot detect condescension, dismissiveness, or a
# technically-polite refusal that reads as contempt. It exists because it is
# free and catches the worst cases; judge escalation is the answer when a
# project needs better, and that is exactly what the opt-in is for.
TOXIC_TERMS: frozenset[str] = frozenset(
    {
        "idiot",
        "stupid",
        "moron",
        "dumbass",
        "worthless",
        "pathetic",
        "shut up",
        "kill yourself",
        "hate you",
        "disgusting",
    }
)


# --- Grounding -------------------------------------------------------------

# Numbers are the highest-signal hallucination detector available without a
# model: a fabricated price, date, percentage or count is both the most common
# hallucination and the most damaging, and checking whether it appears in the
# retrieved context is a string search.
NUMBER_PATTERN = re.compile(r"\b\d[\d,]*(?:\.\d+)?%?\b")

# Numbers too common to be evidence of anything.
GROUNDING_STOPWORDS: frozenset[str] = frozenset({"0", "1", "2", "3", "4", "5", "10", "100"})


@dataclass
class Finding:
    check: str
    severity: float
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"check": self.check, "severity": self.severity, "detail": self.detail}


@dataclass
class CheckInput:
    """What the checks see about one trace."""

    output: str
    inputs: dict[str, Any] = field(default_factory=dict)
    context: list[Any] | None = None


def check_pii(sample: CheckInput) -> Finding | None:
    """Detect personal data or credentials in the model's output.

    Only the *output* is scanned. A user's email in the input is them telling
    you their address; the same email in the output means the model repeated
    someone's data back — which is the leak worth catching.
    """
    hits: dict[str, list[str]] = {}
    worst = 0.0

    for name, pattern in PII_PATTERNS.items():
        matches = pattern.findall(sample.output)
        if not matches:
            continue
        if name == "credit_card":
            matches = [m for m in matches if luhn_valid(m)]
            if not matches:
                continue

        # Redacted in the finding. Storing the raw value would move the leak
        # from a transient trace into the control plane, where it lives longer.
        hits[name] = [_redact(m) for m in matches[:5]]
        worst = max(worst, PII_SEVERITY.get(name, 0.5))

    if not hits:
        return None
    return Finding(check="pii", severity=worst, detail={"matches": hits})


def _redact(value: str) -> str:
    stripped = value.strip()
    if len(stripped) <= 4:
        return "*" * len(stripped)
    return f"{stripped[:2]}{'*' * (len(stripped) - 4)}{stripped[-2:]}"


def check_toxicity(sample: CheckInput) -> Finding | None:
    """Wordlist match against the output. Crude by design — see TOXIC_TERMS."""
    lowered = sample.output.lower()
    hits = [term for term in TOXIC_TERMS if term in lowered]
    if not hits:
        return None
    # Severity scales with how many distinct terms appear, capped. One match is
    # plausibly a quotation; several is not.
    return Finding(
        check="toxicity",
        severity=min(1.0, 0.5 + 0.25 * (len(hits) - 1)),
        detail={"terms": sorted(hits)},
    )


def check_grounding(sample: CheckInput) -> Finding | None:
    """Flag numbers in the output that appear nowhere in the retrieved context.

    A cheap proxy for faithfulness. It cannot detect a fabricated *claim* in
    prose — that needs a judge — but a specific figure with no source is the
    most checkable and most consequential kind of hallucination.

    Returns None when there is no context: this check is meaningless for a
    non-RAG trace, and flagging every one of them would drown the queue.
    """
    if not sample.context:
        return None

    haystack = " ".join(entry if isinstance(entry, str) else str(entry) for entry in sample.context)
    # Normalise separators so "1,000" in the output matches "1000" in context.
    haystack_normalised = haystack.replace(",", "")

    unsupported: list[str] = []
    for match in NUMBER_PATTERN.findall(sample.output):
        cleaned = match.rstrip("%").replace(",", "")
        if cleaned in GROUNDING_STOPWORDS or not cleaned:
            continue
        if cleaned not in haystack_normalised:
            unsupported.append(match)

    if not unsupported:
        return None

    # Severity grows with count but stays below the PII ceiling — an ungrounded
    # number is a quality problem, a leaked key is an incident.
    return Finding(
        check="grounding",
        severity=min(0.8, 0.3 + 0.15 * len(unsupported)),
        detail={"unsupported_numbers": unsupported[:10], "count": len(unsupported)},
    )


CHECKS = {
    "pii": check_pii,
    "grounding": check_grounding,
    "toxicity": check_toxicity,
}


def run_checks(sample: CheckInput, enabled: set[str] | None = None) -> list[Finding]:
    """Run the enabled checks. Returns findings, worst first.

    A failing check is swallowed rather than propagated: one bad regex must not
    stop the sampler from evaluating the rest of the batch, and losing a check's
    opinion is better than losing the whole run.
    """
    active = enabled if enabled is not None else set(CHECKS)
    findings: list[Finding] = []

    for name, check in CHECKS.items():
        if name not in active:
            continue
        try:
            result = check(sample)
        except Exception as exc:
            # Losing one check's opinion beats losing the whole sampling run —
            # but it is logged, because a check that has been silently failing
            # for a month is indistinguishable from one that never fires.
            log.warning("guardrail.check_failed", check=name, error=str(exc))
            continue
        if result is not None:
            findings.append(result)

    return sorted(findings, key=lambda f: f.severity, reverse=True)
