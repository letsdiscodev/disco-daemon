from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import String, Unicode
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from disco.models import (
        CommandRun,
        Deployment,
        ProjectDomain,
        ProjectEnvironmentVariable,
        ProjectGithubRepo,
        ProjectKeyValue,
    )
from disco.models.meta import Base, DateTimeTzAware


class Project(Base):
    __tablename__ = "projects"

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
    name: Mapped[str] = mapped_column(Unicode(255), nullable=False)
    deployment_type: Mapped[str | None] = mapped_column(Unicode(255), nullable=True)

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
    github_repo: Mapped[ProjectGithubRepo] = relationship(
        "ProjectGithubRepo",
        back_populates="project",
        uselist=False,
    )
    domains: Mapped[list[ProjectDomain]] = relationship(
        "ProjectDomain", back_populates="project", order_by="ProjectDomain.name"
    )

    def log(self):
        return f"PROJECT_{self.id} ({self.name})"
