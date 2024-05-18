from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Unicode
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from disco.models import (
        ApiKey,
        CommandRun,
        DeploymentEnvironmentVariable,
        Project,
    )
from disco.models.meta import Base, DateTimeTzAware


class Deployment(Base):
    __tablename__ = "deployments"

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
    number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    commit_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    disco_file: Mapped[str | None] = mapped_column(Unicode(5000), nullable=True)
    project_name: Mapped[str] = mapped_column(Unicode(255), nullable=False)
    github_repo_full_name: Mapped[str | None] = mapped_column(
        Unicode(2048), nullable=True
    )
    registry_host: Mapped[str | None] = mapped_column(Unicode(2048), nullable=True)
    project_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("projects.id"),
        nullable=False,
        index=True,
    )
    prev_deployment_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("deployments.id"),
        nullable=True,
        index=True,
    )
    by_api_key_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("api_keys.id"),
        nullable=True,
        index=True,
    )

    project: Mapped[Project] = relationship(
        "Project",
        back_populates="deployments",
    )
    by_api_key: Mapped[ApiKey | None] = relationship(
        "ApiKey",
        back_populates="deployments",
    )
    prev_deployment: Mapped[Deployment | None] = relationship(
        "Deployment",
    )
    command_runs: Mapped[list[CommandRun]] = relationship(
        "CommandRun", back_populates="deployment", order_by="CommandRun.number.desc()"
    )
    env_variables: Mapped[list[DeploymentEnvironmentVariable]] = relationship(
        "DeploymentEnvironmentVariable", back_populates="deployment"
    )

    def log(self):
        return f"DEPLOYMENT_{self.id} ({self.project_name} {self.number})"
