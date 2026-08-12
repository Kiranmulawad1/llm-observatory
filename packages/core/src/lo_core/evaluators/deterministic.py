"""Deterministic evaluators: no model calls, no network, same answer every time.

These are the evaluators that should carry most of a CI gate. A judge is useful
where correctness is genuinely subjective, but it costs money, adds latency and
is itself non-deterministic — so anything checkable by string equality, a pattern
or a schema should be, with the judge reserved for what actually needs it. That
ordering is a cost and reliability argument, not a purity one.
"""

from __future__ import annotations

import json
from typing import Any

import regex
from jsonschema import Draft202012Validator
from jsonschema import ValidationError as JSONSchemaValidationError
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lo_core.evaluators.base import (
    EvaluationSample,
    Evaluator,
    EvaluatorOutcome,
    UnscoreableError,
)
from lo_core.evaluators.registry import register

# Wall-clock bound on a caller-supplied pattern. See RegexMatchEvaluator.
REGEX_TIMEOUT_SECONDS = 1.0


class ExactMatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_sensitive: bool = True
    # On by default because providers routinely add a trailing newline, and
    # failing every example over invisible whitespace teaches people to distrust
    # the eval rather than fix the prompt.
    strip_whitespace: bool = True


@register
class ExactMatchEvaluator(Evaluator[ExactMatchConfig]):
    name = "exact_match"
    description = "Output equals the expected output exactly. Score is 1.0 or 0.0."
    Config = ExactMatchConfig

    async def evaluate(self, sample: EvaluationSample) -> EvaluatorOutcome:
        expected = self.require_expected(sample)
        actual = sample.output

        if self.config.strip_whitespace:
            expected, actual = expected.strip(), actual.strip()
        if not self.config.case_sensitive:
            expected, actual = expected.casefold(), actual.casefold()

        matched = expected == actual
        return EvaluatorOutcome(
            score=1.0 if matched else 0.0,
            passed=matched,
            detail={} if matched else {"expected": expected, "actual": actual},
        )


class RegexMatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str = Field(min_length=1, max_length=1000)
    ignore_case: bool = False
    # Require the pattern to consume the whole output rather than appear in it.
    full_match: bool = False

    @field_validator("pattern")
    @classmethod
    def _compilable(cls, v: str) -> str:
        try:
            regex.compile(v)
        except regex.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc
        return v


@register
class RegexMatchEvaluator(Evaluator[RegexMatchConfig]):
    """Pattern match against the output.

    **The pattern comes from an API caller, so it is untrusted input.** Stdlib
    `re` cannot bound backtracking, so a pattern like `(a+)+$` against a long
    non-matching output pins a CPU core effectively forever — a denial of service
    against the worker pool, triggered by an ordinary-looking eval config. The
    `regex` module accepts a wall-clock `timeout`, which turns that from an
    outage into a scoring error on a single example.

    The pattern is compiled during config validation too, so a malformed regex is
    a 422 when the run is requested rather than an exception on example one.
    """

    name = "regex_match"
    description = "Output matches a regular expression. Score is 1.0 or 0.0."
    Config = RegexMatchConfig

    def __init__(self, config: BaseModel | None = None) -> None:
        super().__init__(config)
        flags = regex.IGNORECASE if self.config.ignore_case else 0
        self._pattern = regex.compile(self.config.pattern, flags)

    async def evaluate(self, sample: EvaluationSample) -> EvaluatorOutcome:
        matcher = self._pattern.fullmatch if self.config.full_match else self._pattern.search
        try:
            match = matcher(sample.output, timeout=REGEX_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise UnscoreableError(
                f"pattern exceeded {REGEX_TIMEOUT_SECONDS}s against this output; "
                "it likely backtracks catastrophically"
            ) from exc

        matched = match is not None
        detail: dict[str, Any] = {"pattern": self.config.pattern}
        if match is not None:
            detail["matched"] = match.group(0)[:500]

        return EvaluatorOutcome(score=1.0 if matched else 0.0, passed=matched, detail=detail)


class JSONSchemaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Aliased because `schema` collides with BaseModel's own attribute namespace.
    json_schema: dict[str, Any] = Field(
        alias="schema",
        description="A JSON Schema (Draft 2020-12).",
    )
    # Models often emit ```json fences even when told not to. Off by default so
    # the check stays strict, but available when the surrounding application
    # strips fences before using the output anyway.
    strip_code_fences: bool = False

    @field_validator("json_schema")
    @classmethod
    def _valid_schema(cls, v: dict[str, Any]) -> dict[str, Any]:
        try:
            Draft202012Validator.check_schema(v)
        except SchemaError as exc:
            raise ValueError(f"invalid JSON Schema: {exc.message}") from exc
        return v


@register
class JSONSchemaEvaluator(Evaluator[JSONSchemaConfig]):
    """Output parses as JSON and validates against a schema.

    The most useful deterministic check for structured-output prompts: it catches
    "the model wrapped the JSON in prose" and "the model dropped a required
    field" without anyone writing a bespoke parser per prompt.
    """

    name = "json_schema"
    description = "Output is JSON conforming to a JSON Schema. Score is 1.0 or 0.0."
    Config = JSONSchemaConfig

    def __init__(self, config: BaseModel | None = None) -> None:
        super().__init__(config)
        self._validator = Draft202012Validator(self.config.json_schema)

    @staticmethod
    def _strip_fences(text: str) -> str:
        stripped = text.strip()
        if not stripped.startswith("```"):
            return stripped
        lines = stripped.splitlines()
        if len(lines) < 2:
            return stripped
        body = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        return "\n".join(body).strip()

    async def evaluate(self, sample: EvaluationSample) -> EvaluatorOutcome:
        text = self._strip_fences(sample.output) if self.config.strip_code_fences else sample.output

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            # Unparseable output is a real failure of a structured-output prompt,
            # not a dataset gap — so it scores 0.0 rather than raising
            # UnscoreableError, which would exclude it from the mean.
            return EvaluatorOutcome(
                score=0.0,
                passed=False,
                detail={"error": "output is not valid JSON", "reason": str(exc)},
            )

        errors = sorted(self._validator.iter_errors(parsed), key=lambda e: list(e.absolute_path))
        if not errors:
            return EvaluatorOutcome(score=1.0, passed=True, detail={})

        return EvaluatorOutcome(
            score=0.0,
            passed=False,
            detail={
                "error": "output does not match schema",
                # Capped: a badly wrong output against a large schema can produce
                # hundreds of violations, and storing them all bloats every row.
                "violations": [_describe(e) for e in errors[:10]],
                "violation_count": len(errors),
            },
        )


def _describe(error: JSONSchemaValidationError) -> dict[str, Any]:
    path = "$" + "".join(
        f"[{p!r}]" if isinstance(p, str) else f"[{p}]" for p in error.absolute_path
    )
    return {"path": path, "message": error.message}
