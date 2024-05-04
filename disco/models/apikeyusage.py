from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from disco.models import ApiKey
from disco.models.meta import Base, DateTimeTzAware


class ApiKeyUsage(Base):
    __tablename__ = "api_key_usages"

    id: Mapped[str] = mapped_column(
        String(32), default=lambda: uuid.uuid4().hex, primary_key=True
    )
    created: Mapped[datetime] = mapped_column(
        DateTimeTzAware(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    api_key_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("api_keys.id"),
        nullable=False,
        index=True,
    )

    api_key: Mapped[ApiKey] = relationship(
        "ApiKey",
        back_populates="usages",
    )

    def log(self):
        return f"API_KEY_USAGE_{self.id}"
