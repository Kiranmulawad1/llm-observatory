"""Wire contracts for trace ingestion and querying.

This is the one part of the API that a *third party's* code talks to, so the
shapes here are a public contract in a way the rest of the platform's schemas are
not — changing them breaks applications already in production.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from lo_core.db.models.telemetry import (
    SPAN_ID_LENGTH,
    TRACE_ID_LENGTH,
    SpanKind,
    SpanStatus,
)

# W3C Trace Context ids: lowercase hex, fixed width.
TraceId = Annotated[
    str, StringConstraints(pattern=rf"^[0-9a-f]{{{TRACE_ID_LENGTH}}}$", to_lower=True)
]
SpanId = Annotated[
    str, StringConstraints(pattern=rf"^[0-9a-f]{{{SPAN_ID_LENGTH}}}$", to_lower=True)
]

# One request may not carry an unbounded number of spans. A cap keeps a single
# malformed client from producing a request the API must buffer entirely in
# memory before it can reject it.
MAX_SPANS_PER_BATCH = 500


class SpanIngest(BaseModel):
    """One span as the SDK sends it."""

    model_config = ConfigDict(extra="forbid")

    trace_id: TraceId
    span_id: SpanId
    parent_span_id: SpanId | None = None

    name: str = Field(min_length=1, max_length=200)
    kind: SpanKind = "other"
    status: SpanStatus = "ok"

    started_at: datetime
    ended_at: datetime | None = None
    # Measured by the SDK from a monotonic clock. Not derived from the two
    # timestamps here, because wall-clock time can step backwards mid-span and
    # produce a negative duration.
    duration_ms: int | None = Field(default=None, ge=0)

    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model: str | None = Field(default=None, max_length=100)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = None

    prompt_version_id: uuid.UUID | None = None

    error_type: str | None = Field(default=None, max_length=200)
    error_message: str | None = None


class TraceIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spans: list[SpanIngest] = Field(min_length=1, max_length=MAX_SPANS_PER_BATCH)


class TraceIngestResponse(BaseModel):
    accepted: int
    # Spans already stored under the same (trace_id, span_id). The SDK retries on
    # network failure without knowing whether the first attempt landed, so
    # duplicates are expected and counted rather than treated as errors.
    duplicates: int
    traces_touched: int


class SpanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    span_id: str
    trace_id: str
    parent_span_id: str | None
    name: str
    kind: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    duration_ms: int | None
    input: dict[str, Any] | None = Field(default=None, validation_alias="span_input")
    output: dict[str, Any] | None = Field(default=None, validation_alias="span_output")
    metadata: dict[str, Any] = Field(default_factory=dict, validation_alias="span_metadata")
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd: Decimal | None
    prompt_version_id: uuid.UUID | None
    error_type: str | None
    error_message: str | None


class SpanNode(SpanRead):
    """A span with its children attached.

    The tree is assembled server-side from the flat rows. Doing it here rather
    than in the browser means the CLI, the SDK and any future consumer get the
    same structure without each reimplementing the parent-pointer walk.
    """

    children: list[SpanNode] = Field(default_factory=list)


class TraceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    trace_id: str
    project_id: uuid.UUID
    name: str
    status: str
    started_at: datetime
    ended_at: datetime | None
    duration_ms: int | None
    span_count: int
    error_count: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_cost_usd: Decimal | None
    metadata: dict[str, Any] = Field(default_factory=dict, validation_alias="trace_metadata")
    flagged_at: datetime | None


class TraceDetail(TraceRead):
    """A trace plus its span tree."""

    root: SpanNode | None = None
    # Spans whose parent is absent — a partial flush, or a parent still in the
    # SDK's buffer. Surfaced rather than dropped, because silently hiding spans
    # would make a trace look complete when it is not.
    orphans: list[SpanNode] = Field(default_factory=list)


# --- API keys -------------------------------------------------------------


class ApiKeyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(default_factory=lambda: ["ingest"])
    expires_at: datetime | None = None
    description: str | None = None


class ApiKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    key_prefix: str
    scopes: list[str]
    last_used_at: datetime | None
    revoked_at: datetime | None
    expires_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyRead):
    """The only response that ever carries the plaintext key."""

    key: str = Field(description="Shown once. Store it now; it cannot be retrieved again.")
