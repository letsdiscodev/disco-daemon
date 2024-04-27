from datetime import datetime, timezone

from sqlalchemy import String, UnicodeText
from sqlalchemy.orm import Mapped, mapped_column

from disco.models.meta import Base, DateTimeTzAware


class KeyValue(Base):
    __tablename__ = "key_values"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
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
    value: Mapped[str | None] = mapped_column(UnicodeText())
