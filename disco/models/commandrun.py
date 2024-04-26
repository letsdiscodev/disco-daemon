from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Unicode,
    UnicodeText,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from disco.models import ApiKey, Deployment, Project
from disco.models.meta import Base


class CommandRun(Base):
    __tablename__ = "command_runs"

    id: Mapped[str] = mapped_column(
        String(32), default=lambda: uuid.uuid4().hex, primary_key=True
    )
    created: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    service: Mapped[str] = mapped_column(Unicode(), nullable=False)
    command: Mapped[str] = mapped_column(UnicodeText(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    project_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("projects.id"),
        nullable=False,
        index=True,
    )
    deployment_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("deployments.id"),
        nullable=True,
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
        back_populates="command_runs",
    )
    by_api_key: Mapped[ApiKey] = relationship(
        "ApiKey",
        back_populates="command_runs",
    )
    deployment: Mapped[Deployment] = relationship(
        "Deployment",
        back_populates="command_runs",
    )

    def log(self):
        return f"COMMAND_RUN_{self.id} ({self.project_name} {self.number})"
