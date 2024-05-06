from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Unicode
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from disco.models import (
        Project,
    )
from disco.models.meta import Base, DateTimeTzAware


class ProjectDomain(Base):
    __tablename__ = "project_domains"

    id: Mapped[str] = mapped_column(
        String(32), default=lambda: uuid.uuid4().hex, primary_key=True
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
    name: Mapped[str] = mapped_column(
        Unicode(255), nullable=False, index=True, unique=True
    )
    project_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("projects.id"),
        nullable=False,
        index=True,
    )

    project: Mapped[Project] = relationship(
        "Project",
        back_populates="domains",
    )

    def log(self):
        return f"PROJECT_DOMAIN_{self.id} ({self.name})"
