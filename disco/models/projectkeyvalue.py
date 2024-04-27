from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UnicodeText
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from disco.models import (
        Project,
    )
from disco.models.meta import Base, DateTimeTzAware


class ProjectKeyValue(Base):
    __tablename__ = "project_key_values"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("projects.id"),
        primary_key=True,
    )
    created: Mapped[datetime] = mapped_column(
        DateTimeTzAware(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated: Mapped[datetime] = mapped_column(
        DateTimeTzAware(),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    value: Mapped[str | None] = mapped_column(UnicodeText())

    project: Mapped[Project] = relationship(
        "Project",
        back_populates="key_values",
    )

    def log(self):
        return f"PROJECT_KEY_VAL_{self.key} ({self.project.name})"
