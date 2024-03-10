from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from disco.models import (
        GithubAppRepo,
        Project,
    )
from disco.models.meta import Base, DateTimeTzAware


class ProjectGithubRepo(Base):
    __tablename__ = "project_github_repos"

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
    project_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("projects.id"),
        nullable=False,
        index=True,
    )
    github_app_repo_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("github_app_repos.id"),
        nullable=False,
        index=True,
    )

    project: Mapped[Project] = relationship("Project", back_populates="github_repo")
    github_app_repo: Mapped[GithubAppRepo] = relationship(
        "GithubAppRepo", back_populates="project_github_repos"
    )

    def log(self):
        return f"PROJECT_GITHUB_REPO_{self.id} ({self.name})"
