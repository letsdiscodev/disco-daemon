from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, String, Unicode
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from disco.models import (
        CommandRun,
        Deployment,
        ProjectEnvironmentVariable,
        ProjectKeyValue,
    )
from disco.models.meta import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(
        String(32), default=lambda: uuid.uuid4().hex, primary_key=True
    )
    created: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    name: Mapped[str] = mapped_column(Unicode(255), nullable=False)
    domain: Mapped[str | None] = mapped_column(Unicode(255), nullable=True)
    github_repo: Mapped[str | None] = mapped_column(Unicode(2048), nullable=True)
    github_webhook_token: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    github_webhook_secret: Mapped[str | None] = mapped_column(String(32), nullable=True)
    github_host: Mapped[str | None] = mapped_column(Unicode(2048), nullable=True)

    command_runs: Mapped[list[CommandRun]] = relationship(
        "CommandRun", back_populates="project", order_by="CommandRun.number.desc()"
    )
    deployments: Mapped[list[Deployment]] = relationship(
        "Deployment", back_populates="project", order_by="Deployment.number.desc()"
    )
    env_variables: Mapped[list[ProjectEnvironmentVariable]] = relationship(
        "ProjectEnvironmentVariable",
        back_populates="project",
    )
    key_values: Mapped[list[ProjectKeyValue]] = relationship(
        "ProjectKeyValue",
        back_populates="project",
    )

    def log(self):
        return f"PROJECT_{self.id} ({self.name})"
