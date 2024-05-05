from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import Integer, String, Unicode, UnicodeText
from sqlalchemy.orm import Mapped, mapped_column, relationship

from disco.models.meta import Base, DateTimeTzAware

if TYPE_CHECKING:
    from disco.models import GithubAppInstallation


class GithubApp(Base):
    __tablename__ = "github_apps"

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
    slug: Mapped[str] = mapped_column(Unicode(255), nullable=False)
    name: Mapped[str] = mapped_column(Unicode(255), nullable=False)
    webhook_secret: Mapped[str] = mapped_column(String(32), nullable=False)
    pem: Mapped[str] = mapped_column(UnicodeText, nullable=False)
    client_secret: Mapped[str] = mapped_column(String(32), nullable=False)
    html_url: Mapped[str] = mapped_column(Unicode(2000), nullable=False)
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_login: Mapped[str] = mapped_column(Unicode(255), nullable=False)
    owner_type: Mapped[str] = mapped_column(Unicode(255), nullable=False)
    app_info: Mapped[str] = mapped_column(UnicodeText, nullable=False)

    installations: Mapped[list[GithubAppInstallation]] = relationship(
        "GithubAppInstallation",
        back_populates="github_app",
    )

    def log(self):
        return f"GITHUB_APP_{self.id} ({self.name})"
