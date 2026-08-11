"""Prompt registry: identity, immutable versions, and movable labels.

The shape here is deliberately the same one container registries and model
registries converged on, because the problem is the same:

    Prompt          a stable name people refer to           ("support-triage")
    PromptVersion   an immutable snapshot of content        (version 7)
    PromptLabel     a movable pointer to one version        ("production" -> 7)

Why versions are immutable: an eval run, and later a production trace, records
*which prompt version produced it*. If a version's text could be edited in place,
every historical result silently starts lying about what was actually run —
which destroys the one thing this platform exists to provide. Editing a prompt
therefore always appends a new version; nothing is ever updated.

Why labels are a separate table rather than a column on the version: promotion
has to be atomic. With a `label` column, moving "production" from v6 to v7 is two
UPDATEs, and between them the registry either shows two production versions or
none. As a row keyed by `(prompt_id, label)`, promotion is a single upsert that
readers observe as one instantaneous change.
"""

from __future__ import annotations

# Imported at runtime, not under TYPE_CHECKING: SQLAlchemy evaluates mapped
# column annotations to pick a column type, so `uuid` must genuinely exist in
# this module's namespace. Relationship annotations are the exception — those
# are resolved by class name against the registry, so `Project` below can stay
# type-checking-only.
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lo_core.db.base import CONTROL_SCHEMA, ControlBase
from lo_core.db.mixins import CreatedAtMixin, TimestampMixin, UUIDPrimaryKey

if TYPE_CHECKING:
    from lo_core.db.models.project import Project

# A label is any URL-safe token. "production" / "staging" / "experimental" are
# the conventional three, but teams invent their own ("canary", "eu-rollout"),
# and a hardcoded enum would force a migration every time they do.
LABEL_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"
SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"


class Prompt(UUIDPrimaryKey, TimestampMixin, ControlBase):
    """The stable identity. Holds no content of its own."""

    __tablename__ = "prompts"
    __table_args__ = (
        # Slugs are unique per project, not globally: two teams should both be
        # able to own a prompt called "summarize" without coordinating names.
        UniqueConstraint("project_id", "slug", name="uq_prompts_project_id_slug"),
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="slug_format"),
        {"schema": CONTROL_SCHEMA},
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    project: Mapped[Project] = relationship(back_populates="prompts", lazy="raise")
    versions: Mapped[list[PromptVersion]] = relationship(
        back_populates="prompt",
        cascade="all, delete-orphan",
        order_by="desc(PromptVersion.version)",
        lazy="raise",
    )
    labels: Mapped[list[PromptLabel]] = relationship(
        back_populates="prompt",
        cascade="all, delete-orphan",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<Prompt {self.slug}>"


class PromptVersion(UUIDPrimaryKey, CreatedAtMixin, ControlBase):
    """An immutable snapshot. Append-only; never updated, never deleted.

    Note the absence of `updated_at` — see CreatedAtMixin. Immutability is
    enforced in the service layer and by the fact that nothing in the codebase
    issues an UPDATE against this table.
    """

    __tablename__ = "prompt_versions"
    __table_args__ = (
        # The monotonic version number is unique per prompt. This constraint is
        # not just documentation: it is the backstop that turns a lost update
        # race into a caught integrity error instead of two rows claiming to be
        # version 4. The service layer additionally serialises writers with a
        # row lock so the error path is never hit under normal load.
        UniqueConstraint("prompt_id", "version", name="uq_prompt_versions_prompt_id_version"),
        CheckConstraint("version >= 1", name="version_positive"),
        # Locating "the version whose content hash is X" is how a CI job avoids
        # creating a duplicate version when a prompt file has not actually
        # changed between commits.
        Index("ix_prompt_versions_prompt_id_content_hash", "prompt_id", "content_hash"),
        {"schema": CONTROL_SCHEMA},
    )

    prompt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.prompts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    # Ordered chat messages: [{"role": "system"|"user"|"assistant", "content": "..."}].
    # Stored as the provider-shaped structure rather than one flat string so the
    # system instruction is versioned and diffable separately from the user turn.
    messages: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)

    # Declared template variables, derived from the Jinja source at write time:
    # [{"name": "question", "required": true}]. Persisted rather than re-parsed
    # on every render so the API can validate a caller's inputs, and the UI can
    # show a form, without compiling the template.
    variables: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )

    # Model + decoding parameters that belong *with* this text. Temperature and
    # model choice change output as much as wording does, so they are part of the
    # version rather than a separate knob someone forgets to record.
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")

    # SHA-256 over the canonicalised (messages, parameters) pair. Lets a caller
    # ask "has this content already been registered?" without a deep JSONB
    # comparison. Not unique: reverting to an earlier version's exact content is
    # a legitimate new version, and the history should show it happened.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Provenance. `commit_sha` is what ties a prompt version back to the code
    # change that introduced it, which is how a regression gets bisected.
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    prompt: Mapped[Prompt] = relationship(back_populates="versions", lazy="raise")

    def __repr__(self) -> str:
        return f"<PromptVersion v{self.version}>"


class PromptLabel(UUIDPrimaryKey, TimestampMixin, ControlBase):
    """A movable pointer from a name like "production" to one version."""

    __tablename__ = "prompt_labels"
    __table_args__ = (
        # The heart of the design: at most one "production" per prompt, enforced
        # by the database rather than by application discipline.
        UniqueConstraint("prompt_id", "label", name="uq_prompt_labels_prompt_id_label"),
        CheckConstraint(f"label ~ '{LABEL_PATTERN}'", name="label_format"),
        {"schema": CONTROL_SCHEMA},
    )

    prompt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.prompts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label: Mapped[str] = mapped_column(String(32), nullable=False)

    # RESTRICT, not CASCADE: a version that something currently points at must
    # not be removable out from under the label. Versions are never deleted in
    # normal operation, so this only fires against a manual cleanup mistake —
    # which is exactly when a foreign key should be the thing that objects.
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.prompt_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    updated_by: Mapped[str | None] = mapped_column(String(200), nullable=True)

    prompt: Mapped[Prompt] = relationship(back_populates="labels", lazy="raise")
    version: Mapped[PromptVersion] = relationship(lazy="raise")

    def __repr__(self) -> str:
        return f"<PromptLabel {self.label}>"
