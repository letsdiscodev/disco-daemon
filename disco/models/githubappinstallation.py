from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    ForeignKey,
    Integer,
    UnicodeText,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from disco.models.meta import Base, DateTimeTzAware

if TYPE_CHECKING:
    from disco.models import GithubApp, GithubAppRepo


class GithubAppInstallation(Base):
    __tablename__ = "github_app_installations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # provided by Github
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
    access_token: Mapped[str | None] = mapped_column(UnicodeText, nullable=True)
    access_token_expires: Mapped[datetime | None] = mapped_column(
        DateTimeTzAware, nullable=True
    )
    github_app_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("github_apps.id"),
        nullable=False,
        index=True,
    )

    github_app: Mapped[GithubApp] = relationship(
        "GithubApp",
        back_populates="installations",
    )
    github_app_repos: Mapped[list[GithubAppRepo]] = relationship(
        "GithubAppRepo",
        back_populates="installation",
    )

    def log(self):
        return f"GITHUB_APP_INSTALLATION_{self.id}"
