from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Unicode
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from disco.models import (
        ApiKey,
        Project,
    )
from disco.models.meta import Base, DateTimeTzAware


class ProjectEnvironmentVariable(Base):
    __tablename__ = "project_env_variables"

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
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    value: Mapped[str] = mapped_column(Unicode(4000), nullable=False)
    project_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("projects.id"),
        nullable=False,
        index=True,
    )
    by_api_key_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("api_keys.id"),
        nullable=False,
        index=True,
    )

    project: Mapped[Project] = relationship(
        "Project",
        back_populates="env_variables",
    )
    by_api_key: Mapped[ApiKey] = relationship(
        "ApiKey",
        back_populates="env_variables",
    )

    def log(self):
        return f"PROJECT_ENV_VAR_{self.name}"
