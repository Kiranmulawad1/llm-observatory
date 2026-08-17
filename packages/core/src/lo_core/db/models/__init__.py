"""Mapped classes.

Every model must be imported here. Alembic's autogenerate compares the database
against whatever is registered on the shared MetaData, so a model that is defined
but never imported is invisible to it — and the migration that "forgot" a table
is indistinguishable from a migration that intends to drop it.
"""

from lo_core.db.models.alerting import AlertRule
from lo_core.db.models.api_key import ApiKey
from lo_core.db.models.dataset import Dataset, DatasetItem, DatasetVersion
from lo_core.db.models.evaluation import (
    DeadLetterJob,
    EvalResult,
    EvalRun,
    EvalScore,
)
from lo_core.db.models.project import Project
from lo_core.db.models.prompt import Prompt, PromptLabel, PromptVersion
from lo_core.db.models.telemetry import Span, Trace

__all__ = [
    "AlertRule",
    "ApiKey",
    "Dataset",
    "DatasetItem",
    "DatasetVersion",
    "DeadLetterJob",
    "EvalResult",
    "EvalRun",
    "EvalScore",
    "Project",
    "Prompt",
    "PromptLabel",
    "PromptVersion",
    "Span",
    "Trace",
]
