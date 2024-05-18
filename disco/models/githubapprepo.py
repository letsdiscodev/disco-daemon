from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    ForeignKey,
    Integer,
    String,
    Unicode,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from disco.models.meta import Base, DateTimeTzAware

if TYPE_CHECKING:
    from disco.models import GithubAppInstallation


class GithubAppRepo(Base):
    __tablename__ = "github_app_repos"

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
    installation_id: Mapped[str] = mapped_column(
        Integer,
        ForeignKey("github_app_installations.id"),
        nullable=False,
        index=True,
    )
    full_name: Mapped[str] = mapped_column(Unicode(255), nullable=False, index=True)

    installation: Mapped[GithubAppInstallation] = relationship(
        "GithubAppInstallation",
        back_populates="github_app_repos",
    )

    def log(self):
        return f"GITHUB_APP_REPO_{self.id} ({self.full_name})"
