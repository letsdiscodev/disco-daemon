from __future__ import annotations

from datetime import datetime
from secrets import token_hex
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Unicode
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from disco.models import ApiKey
from disco.models.meta import Base


class ApiKeyInvite(Base):
    __tablename__ = "api_key_invites"

    id: Mapped[str] = mapped_column(
        String(32), default=lambda: token_hex(16), primary_key=True
    )
    created: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    name: Mapped[str] = mapped_column(Unicode(255), nullable=False)
    expires: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    by_api_key_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("api_keys.id"),
        nullable=False,
        index=True,
    )
    api_key_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("api_keys.id"),
        nullable=True,
        index=True,
    )

    by_api_key: Mapped[ApiKey] = relationship(
        "ApiKey",
        foreign_keys=by_api_key_id,
        back_populates="created_api_key_invites",
    )
    api_key: Mapped[ApiKey | None] = relationship(
        "ApiKey",
        foreign_keys=api_key_id,
        back_populates="from_invite",
    )

    def log(self):
        return f"API_KEY_INVITE_{self.id} ({self.name})"
