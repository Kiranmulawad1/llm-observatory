"""The evaluator contract.

An evaluator answers one question about one generated output and returns a score
normalised to 0.0-1.0. Normalisation is the load-bearing constraint: it is what
lets a boolean exact-match, a cosine similarity and (in Phase 4) a judge's 1-5
rubric rating share a single aggregation path, a single storage column and a
single regression-comparison view. An evaluator returning raw units would force
every consumer downstream to special-case it.

Every evaluator is async even when its work is pure CPU. A uniform interface
means the runner has one code path; a sync/async split would force it to know
which kind it is calling, and the deterministic evaluators are fast enough that
the coroutine overhead is irrelevant next to the network call that produced the
output being scored.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, ClassVar, cast

from pydantic import BaseModel, ConfigDict


@dataclass(frozen=True)
class EvaluationSample:
    """Everything an evaluator is allowed to see about one example.

    Frozen so an evaluator cannot mutate the sample and affect the evaluators
    that run after it — they all receive the same object.
    """

    output: str
    inputs: dict[str, Any] = field(default_factory=dict)
    expected_output: str | None = None
    expected_context: list[Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class EvaluatorOutcome(BaseModel):
    """One evaluator's verdict."""

    model_config = ConfigDict(extra="forbid")

    score: float
    # Null when the evaluator has no notion of a threshold.
    passed: bool | None = None
    # Evidence: the matched group, the cosine value, the schema violation. This
    # is what turns "0.0" into something a human can act on.
    detail: dict[str, Any] = {}


class EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UnscoreableError(Exception):
    """This evaluator cannot score this sample.

    Raised for a missing expected output or output that is structurally
    unparseable — a *gap*, not a failure. The runner records it as a null score
    with a reason rather than as 0.0, so a dataset gap never masquerades as a
    quality regression in the run's mean.
    """


class Evaluator[ConfigT: BaseModel](abc.ABC):
    """Base class for all evaluators.

    Subclasses declare a `name` (the stable identifier stored on every score row
    and referenced in API payloads) and a `Config` model. Config is a Pydantic
    model rather than a loose dict so that an invalid evaluator configuration is
    rejected when the run is *requested*, not discovered on example 300 of 500
    after the provider has already been paid.

    Generic over its config type so `self.config` is precisely typed inside each
    subclass — `Evaluator[ExactMatchConfig]` gives `self.config.case_sensitive`
    without a cast at every use site.
    """

    name: ClassVar[str]
    Config: ClassVar[type[BaseModel]] = EmptyConfig
    # Human-readable, surfaced by GET /evaluators so the UI can build a form.
    description: ClassVar[str] = ""

    config: ConfigT

    def __init__(self, config: BaseModel | None = None) -> None:
        # The cast is the one unavoidable seam: `Config` is a ClassVar chosen at
        # runtime by the registry, so the type system cannot connect it to the
        # class's own ConfigT parameter. `build()` validates against exactly this
        # class's Config, so the correspondence holds in practice.
        self.config = cast(ConfigT, config if config is not None else self.Config())

    @abc.abstractmethod
    async def evaluate(self, sample: EvaluationSample) -> EvaluatorOutcome:
        """Score one sample. Raise UnscoreableError if the sample lacks what is needed."""

    @staticmethod
    def require_expected(sample: EvaluationSample) -> str:
        """Helper for evaluators that compare against a reference answer."""
        if sample.expected_output is None:
            raise UnscoreableError("dataset item has no expected_output")
        return sample.expected_output
