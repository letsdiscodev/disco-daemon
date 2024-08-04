from __future__ import annotations

from datetime import datetime, timezone
from secrets import token_hex
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Unicode
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from disco.models import ApiKey
from disco.models.meta import Base, DateTimeTzAware


class CorsOrigin(Base):
    __tablename__ = "cors_origins"

    id: Mapped[str] = mapped_column(
        String(32), default=lambda: token_hex(16), primary_key=True
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
    origin: Mapped[str] = mapped_column(
        Unicode(255), nullable=False, index=True, unique=True
    )
    by_api_key_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("api_keys.id"),
        nullable=False,
        index=True,
    )

    by_api_key: Mapped[ApiKey] = relationship(
        "ApiKey",
        foreign_keys=by_api_key_id,
        back_populates="created_cors_origins",
    )

    def log(self):
        return f"CORS_ORIGIN_{self.id} ({self.origin})"
