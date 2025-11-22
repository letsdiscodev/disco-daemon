import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Unicode
from sqlalchemy.orm import Mapped, mapped_column

from disco.models.meta import Base, DateTimeTzAware


class PendingGithubApp(Base):
    __tablename__ = "pending_github_apps"

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
    expires: Mapped[datetime] = mapped_column(
        DateTimeTzAware(),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    setup_url: Mapped[str] = mapped_column(Unicode(1000), nullable=True)
    organization: Mapped[str] = mapped_column(Unicode(250), nullable=True)

    def log(self):
        return f"PENDING_GITHUB_APP_{self.id}"
