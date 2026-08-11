"""Wire contracts for the prompt registry.

These are deliberately separate types from the SQLAlchemy models. Returning ORM
objects straight out of an API couples the public contract to the physical
schema: a column rename becomes a breaking API change, and a lazy relationship
becomes an accidental N+1 during serialisation. The split costs a few lines of
mapping and buys the freedom to evolve either side independently.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from lo_core.db.models.prompt import LABEL_PATTERN, SLUG_PATTERN

Role = Literal["system", "user", "assistant"]

Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN, min_length=1, max_length=64)]
Label = Annotated[str, StringConstraints(pattern=LABEL_PATTERN, min_length=1, max_length=32)]


class Message(BaseModel):
    """One templated turn. `content` is a Jinja2 template, not a rendered string."""

    model_config = ConfigDict(extra="forbid")

    role: Role
    content: str


class TemplateVariable(BaseModel):
    """A variable discovered in the template source.

    Derived by static analysis at write time rather than declared by the caller,
    so the record cannot drift out of sync with the template it describes.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    required: bool = True


class ModelParameters(BaseModel):
    """Decoding parameters versioned alongside the text.

    `extra="allow"` on purpose: providers keep adding knobs (`top_k`,
    `reasoning_effort`, …) and a registry that rejects an unrecognised parameter
    would need a release every time one ships. Unknown keys are stored verbatim
    and passed through to the provider.
    """

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)


# --- Projects -------------------------------------------------------------


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: Slug
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


# --- Prompts --------------------------------------------------------------


class PromptCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: Slug
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class PromptUpdate(BaseModel):
    """Only the descriptive metadata is mutable. Content lives in versions."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class PromptVersionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[Message] = Field(min_length=1)
    parameters: ModelParameters = Field(default_factory=ModelParameters)
    commit_sha: str | None = Field(default=None, max_length=40)
    created_by: str | None = Field(default=None, max_length=200)
    change_note: str | None = None


class PromptVersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    prompt_id: uuid.UUID
    version: int
    messages: list[Message]
    variables: list[TemplateVariable]
    parameters: dict[str, Any]
    content_hash: str
    commit_sha: str | None
    created_by: str | None
    change_note: str | None
    created_at: datetime


class PromptLabelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    label: str
    version_id: uuid.UUID
    version: int
    updated_by: str | None
    updated_at: datetime


class PromptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    slug: str
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    latest_version: int | None = None
    labels: list[PromptLabelRead] = Field(default_factory=list)


class LabelAssign(BaseModel):
    """Point a label at a version. Idempotent: re-assigning the same version is a no-op."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    updated_by: str | None = Field(default=None, max_length=200)


# --- Rendering ------------------------------------------------------------


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    variables: dict[str, Any] = Field(default_factory=dict)


class RenderedMessage(BaseModel):
    role: Role
    content: str


class RenderResponse(BaseModel):
    version: int
    messages: list[RenderedMessage]
    parameters: dict[str, Any]


# --- Diffs ----------------------------------------------------------------

ChangeKind = Literal["added", "removed", "modified", "unchanged"]


class MessageDiff(BaseModel):
    """Per-message diff. Messages are positional, so index identifies the turn."""

    index: int
    change: ChangeKind
    role_from: Role | None = None
    role_to: Role | None = None
    content_from: str | None = None
    content_to: str | None = None
    # Unified diff of this message's content, empty when unchanged.
    unified: str = ""


class ParameterDiff(BaseModel):
    key: str
    change: ChangeKind
    value_from: Any = None
    value_to: Any = None


class PromptDiff(BaseModel):
    """Structured, not a rendered blob.

    Computed server-side so the web UI, the SDK and a CI regression gate all read
    the same diff rather than each reimplementing one. Structured rather than a
    pre-formatted string so the frontend can style it, and so a machine consumer
    can answer "did the system prompt change?" without parsing text.
    """

    prompt_id: uuid.UUID
    from_version: int
    to_version: int
    identical: bool
    messages: list[MessageDiff]
    parameters: list[ParameterDiff]
