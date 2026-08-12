"""Evaluators.

Importing this package registers every built-in evaluator. `registry` is the
only supported way to construct one — see registry.py for why discovery is a
plain dict rather than entry points.
"""

from lo_core.evaluators.base import (
    EvaluationSample,
    Evaluator,
    EvaluatorOutcome,
    UnscoreableError,
)
from lo_core.evaluators.deterministic import (
    ExactMatchEvaluator,
    JSONSchemaEvaluator,
    RegexMatchEvaluator,
)
from lo_core.evaluators.registry import (
    EvaluatorInfo,
    EvaluatorSpec,
    available,
    build,
    build_all,
    get,
    register,
)
from lo_core.evaluators.similarity import EmbeddingSimilarityEvaluator

__all__ = [
    "EmbeddingSimilarityEvaluator",
    "EvaluationSample",
    "Evaluator",
    "EvaluatorInfo",
    "EvaluatorOutcome",
    "EvaluatorSpec",
    "ExactMatchEvaluator",
    "JSONSchemaEvaluator",
    "RegexMatchEvaluator",
    "UnscoreableError",
    "available",
    "build",
    "build_all",
    "get",
    "register",
]
