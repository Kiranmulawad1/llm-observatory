"""Projects — the tenancy boundary.

Every other table in the control plane hangs off a project, and Phase 8 will
issue API keys scoped to one. The table exists now, well before authentication
does, because adding a tenancy column to a populated schema later means a
migration that has to invent a project for every pre-existing row. Getting the
foreign key in before there is any data is free; retrofitting it is not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lo_core.db.base import ControlBase
from lo_core.db.mixins import TimestampMixin, UUIDPrimaryKey

if TYPE_CHECKING:
    from lo_core.db.models.prompt import Prompt


class Project(UUIDPrimaryKey, TimestampMixin, ControlBase):
    __tablename__ = "projects"

    # Human-typed, URL-safe identifier. Callers reference projects by slug in the
    # API so that integration code and CI configuration stay readable, rather
    # than carrying an opaque UUID around in every request.
    slug: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    prompts: Mapped[list[Prompt]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        # Deleting a project is rare and deliberate; never load the full prompt
        # list just because a project was fetched.
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<Project {self.slug}>"
