"""Evaluator registry.

A name-to-class mapping populated by the `@register` decorator at import time.

Deliberately *not* Python entry points. Entry-point discovery is the right answer
when third parties ship evaluators as separate distributions, and it is the
documented upgrade path — but with a single in-repo consumer it would buy
nothing and cost real things: evaluators would become invisible to static
analysis, a typo'd entry point would fail at runtime instead of import, and the
plugin surface would have to be treated as a public API before anyone has asked
for one. The registry is one dict; swapping its population strategy later is a
contained change because `build` is the only way anything constructs an
evaluator.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lo_core.errors import ValidationError as DomainValidationError
from lo_core.evaluators.base import Evaluator

_REGISTRY: dict[str, type[Evaluator[Any]]] = {}


def register(cls: type[Evaluator[Any]]) -> type[Evaluator[Any]]:
    """Class decorator that adds an evaluator to the registry.

    Rejects duplicate names loudly at import time. A silent overwrite would mean
    two evaluators sharing a name, and every stored score row referencing that
    name becoming ambiguous after the fact.
    """
    name = cls.name
    existing = _REGISTRY.get(name)
    if existing is not None and existing is not cls:
        raise RuntimeError(
            f"evaluator name {name!r} is already registered to {existing.__qualname__}"
        )
    _REGISTRY[name] = cls
    return cls


class EvaluatorSpec(BaseModel):
    """A request for one configured evaluator, as it appears in an API payload."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(description="Registered evaluator name, e.g. 'exact_match'")
    config: dict[str, Any] = Field(default_factory=dict)


class EvaluatorInfo(BaseModel):
    """Registry entry as exposed by the API, so a UI can render a config form."""

    type: str
    description: str
    config_schema: dict[str, Any]


def available() -> list[EvaluatorInfo]:
    return [
        EvaluatorInfo(
            type=name,
            description=cls.description,
            config_schema=cls.Config.model_json_schema(),
        )
        for name, cls in sorted(_REGISTRY.items())
    ]


def get(name: str) -> type[Evaluator[Any]]:
    cls = _REGISTRY.get(name)
    if cls is None:
        known = ", ".join(sorted(_REGISTRY)) or "none registered"
        raise DomainValidationError(f"unknown evaluator {name!r}; available: {known}")
    return cls


def build(spec: EvaluatorSpec) -> Evaluator[Any]:
    """Instantiate a configured evaluator, validating its config.

    Called when a run is *requested*, so a bad regex or a missing threshold is a
    422 on the API call rather than a job that dies partway through after paying
    for a few hundred completions.
    """
    cls = get(spec.type)
    try:
        config = cls.Config.model_validate(spec.config)
    except ValidationError as exc:
        raise DomainValidationError(f"invalid config for evaluator {spec.type!r}: {exc}") from exc
    return cls(config)


def build_all(specs: list[EvaluatorSpec]) -> list[Evaluator[Any]]:
    if not specs:
        raise DomainValidationError("at least one evaluator is required")

    seen: set[str] = set()
    for spec in specs:
        if spec.type in seen:
            # Scores are stored uniquely per (result, evaluator name), so the
            # same evaluator twice would collide on insert. Rejecting here gives
            # a clear message instead of an integrity error mid-run.
            raise DomainValidationError(f"evaluator {spec.type!r} specified more than once")
        seen.add(spec.type)

    return [build(spec) for spec in specs]
