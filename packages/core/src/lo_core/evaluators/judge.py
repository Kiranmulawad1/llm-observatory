"""LLM-as-judge evaluator.

Scores an output against a rubric by asking a model. Used where correctness is
genuinely subjective — faithfulness to a context, relevance to a question — and
no deterministic check can express the question.

It is deliberately the *last* evaluator to reach for. It costs money per example,
adds seconds of latency, and is itself non-deterministic, so anything a regex or
a schema can answer should be answered that way (see evaluators/deterministic.py).

Three things make the judged score trustworthy enough to gate on:

**The rubric is a registered prompt version.** Not a string in this file. The run
records `judge_prompt_version_id`, so a score drop is attributable to either the
model under test or the rubric — not ambiguously both.

**The response is schema-constrained.** No parsing prose for a number.

**Self-judging is recorded.** A model scoring its own output rates it higher.
When the judge model equals the model under test, the score detail says so, so an
unusually high number is traceable rather than merely flattering.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from lo_core.evaluators.base import (
    EvaluationSample,
    Evaluator,
    EvaluatorOutcome,
    UnscoreableError,
)
from lo_core.evaluators.registry import register
from lo_core.evaluators.rubrics import (
    JUDGE_RESPONSE_SCHEMA,
    JUDGE_SCALE_MAX,
    JUDGE_SCALE_MIN,
)
from lo_core.providers.base import GenerationProvider, GenerationRequest
from lo_core.schemas.prompt import Message, RenderedMessage
from lo_core.templating import render_messages

# Judged scores are noisy near the boundary, so the default pass mark sits at 4/5
# — "correct with a minor omission" — rather than at the midpoint.
DEFAULT_PASS_THRESHOLD = 0.75


class JudgeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Slug of a `kind="judge"` prompt in this project's registry.
    rubric: str = Field(description="Judge prompt slug, e.g. 'judge-faithfulness'")
    # Version number or label. Defaults to the rubric's production label so a
    # promoted rubric applies without editing every run config.
    rubric_version: str | None = None
    threshold: float = Field(default=DEFAULT_PASS_THRESHOLD, ge=0.0, le=1.0)
    # Which dataset field holds the retrieved context, for rubrics that use it.
    context_field: str = "context"


@register
class JudgeEvaluator(Evaluator[JudgeConfig]):
    name = "llm_judge"
    description = (
        "Scores an output against a versioned rubric using an LLM judge. "
        "The judge returns a 1-5 rating, normalised to 0.0-1.0."
    )
    Config = JudgeConfig

    def __init__(self, config: BaseModel | None = None) -> None:
        super().__init__(config)
        self._provider: GenerationProvider | None = None
        self._template: list[Message] | None = None
        self._model: str | None = None
        self._target_model: str | None = None

    def bind(
        self,
        provider: GenerationProvider,
        template: list[Message],
        model: str,
        target_model: str | None = None,
    ) -> None:
        """Attach the judge's provider and its resolved rubric template.

        Resolved by the runner, not here: this class has no database access, and
        keeping it that way is what lets the rubric be any registered version
        without the evaluator knowing how versions are stored.
        """
        self._provider = provider
        self._template = template
        self._model = model
        self._target_model = target_model

    def _render(self, sample: EvaluationSample, template: list[Message]) -> list[RenderedMessage]:
        context = sample.inputs.get(self.config.context_field)
        if context is None and sample.expected_context is not None:
            context = sample.expected_context

        # Dataset fields first, derived variables second — the order matters.
        #
        # A RAG dataset's item has a `context` field holding the raw passage
        # list. If it were spread last it would overwrite the formatted,
        # numbered context with a Python repr (`['doc a', 'doc b']`), and the
        # rubric would silently judge against that. The derived values are the
        # ones the rubric's contract promises, so they win.
        variables: dict[str, Any] = {
            **sample.inputs,
            "output": sample.output,
            "expected_output": sample.expected_output or "",
            "inputs": json.dumps(sample.inputs, ensure_ascii=False, indent=2),
            "context": _stringify_context(context),
        }
        return render_messages(template, variables)

    async def evaluate(self, sample: EvaluationSample) -> EvaluatorOutcome:
        if self._provider is None or self._template is None or self._model is None:
            raise RuntimeError("llm_judge evaluator was not bound to a provider and rubric")

        messages = self._render(sample, self._template)

        response = await self._provider.generate(
            GenerationRequest(
                messages=messages,
                model=self._model,
                max_tokens=1024,
                response_schema=JUDGE_RESPONSE_SCHEMA,
            )
        )

        try:
            verdict = json.loads(response.text)
            raw_score = int(verdict["score"])
            reasoning = str(verdict.get("reasoning", ""))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            # Structured outputs make this close to impossible, but a provider
            # that ignores the schema must not be scored as 0.0 — that would
            # read as "the answer was bad" when it means "the judge failed".
            raise UnscoreableError(f"judge returned unusable output: {exc}") from exc

        clamped = max(JUDGE_SCALE_MIN, min(JUDGE_SCALE_MAX, raw_score))
        # Map 1-5 onto 0.0-1.0 so judged scores aggregate alongside every other
        # evaluator. 1 becomes 0.0, not 0.2 — the bottom of the rubric is the
        # bottom of the scale.
        score = (clamped - JUDGE_SCALE_MIN) / (JUDGE_SCALE_MAX - JUDGE_SCALE_MIN)

        detail: dict[str, Any] = {
            "raw_score": clamped,
            "scale": f"{JUDGE_SCALE_MIN}-{JUDGE_SCALE_MAX}",
            "reasoning": reasoning,
            "judge_model": response.model,
            "rubric": self.config.rubric,
        }
        if self._target_model and response.model == self._target_model:
            # Recorded, not blocked: sometimes you genuinely want it, and a
            # hidden caveat is worse than a visible one.
            detail["self_judged"] = True
            detail["self_judged_note"] = (
                "judge and evaluated model are the same; models rate their own "
                "output more favourably"
            )

        return EvaluatorOutcome(
            score=score,
            passed=score >= self.config.threshold,
            detail=detail,
        )


def _stringify_context(context: Any) -> str:
    """Flatten retrieved context into the string a rubric renders.

    Passages are numbered because faithfulness reasoning routinely refers to
    "passage 2", and an unnumbered blob gives the judge nothing to point at.
    """
    if context is None:
        return ""
    if isinstance(context, str):
        return context
    if isinstance(context, list):
        parts = []
        for index, entry in enumerate(context, start=1):
            text = entry if isinstance(entry, str) else json.dumps(entry, ensure_ascii=False)
            parts.append(f"[{index}] {text}")
        return "\n\n".join(parts)
    return json.dumps(context, ensure_ascii=False)
