"""Evaluation datasets, versioned immutably.

Datasets follow the same identity/version split as prompts (see ADR 0004), and
for the same reason: an eval run pins a specific dataset version, so comparing
run 41 against run 38 is only meaningful if neither run's dataset can change
underneath it. A mutable dataset turns every historical comparison into
apples-to-oranges without anyone noticing.

Items belong to a *version*, not to the dataset. Adding an example therefore
creates a new version containing the full item set, rather than appending a row
that silently changes what older runs were scored against.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lo_core.db.base import CONTROL_SCHEMA, ControlBase
from lo_core.db.mixins import CreatedAtMixin, TimestampMixin, UUIDPrimaryKey
from lo_core.db.models.prompt import SLUG_PATTERN

if TYPE_CHECKING:
    from lo_core.db.models.project import Project


class Dataset(UUIDPrimaryKey, TimestampMixin, ControlBase):
    """Stable identity for a collection of eval examples."""

    __tablename__ = "datasets"
    __table_args__ = (
        UniqueConstraint("project_id", "slug", name="uq_datasets_project_id_slug"),
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

    project: Mapped[Project] = relationship(lazy="raise")
    versions: Mapped[list[DatasetVersion]] = relationship(
        back_populates="dataset",
        cascade="all, delete-orphan",
        order_by="desc(DatasetVersion.version)",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<Dataset {self.slug}>"


class DatasetVersion(UUIDPrimaryKey, CreatedAtMixin, ControlBase):
    """An immutable snapshot of a full item set."""

    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("dataset_id", "version", name="uq_dataset_versions_dataset_id_version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("item_count >= 0", name="item_count_non_negative"),
        {"schema": CONTROL_SCHEMA},
    )

    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    # Denormalised so the UI can list versions without counting rows per version.
    # Safe to denormalise precisely because items are immutable once written.
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")

    # SHA-256 over the canonicalised item list. Lets a CI job that re-uploads an
    # unchanged dataset file detect that fact and skip creating a version.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    change_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    dataset: Mapped[Dataset] = relationship(back_populates="versions", lazy="raise")
    items: Mapped[list[DatasetItem]] = relationship(
        back_populates="dataset_version",
        cascade="all, delete-orphan",
        order_by="DatasetItem.item_index",
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<DatasetVersion v{self.version} ({self.item_count} items)>"


class DatasetItem(UUIDPrimaryKey, CreatedAtMixin, ControlBase):
    """One eval example.

    `inputs` is a JSON object of template variables rather than a bare string,
    because a prompt version is a template: evaluating it means rendering it with
    named variables. A flat-string dataset would only work for single-variable
    prompts and would have to be reshaped the moment a prompt took both a
    question and a retrieved context.
    """

    __tablename__ = "dataset_items"
    __table_args__ = (
        UniqueConstraint(
            "dataset_version_id", "item_index", name="uq_dataset_items_version_id_item_index"
        ),
        CheckConstraint("item_index >= 0", name="item_index_non_negative"),
        {"schema": CONTROL_SCHEMA},
    )

    dataset_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(f"{CONTROL_SCHEMA}.dataset_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Stable position within the version. Eval results reference items by id, but
    # the index is what makes two runs over the same dataset line up row-by-row
    # in a comparison view.
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)

    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected_output: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Ground-truth passages for retrieval metrics (precision@k, recall@k, MRR).
    # Unused until Phase 4, but part of the item's identity — adding it later
    # would mean a new dataset version for every existing row.
    expected_context: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)

    item_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )

    dataset_version: Mapped[DatasetVersion] = relationship(back_populates="items", lazy="raise")

    def __repr__(self) -> str:
        return f"<DatasetItem #{self.item_index}>"
