from datetime import datetime

from sqlalchemy import DateTime, String, UnicodeText
from sqlalchemy.orm import Mapped, mapped_column

from disco.models.meta import Base


class KeyValue(Base):
    __tablename__ = "key_values"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    created: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    value: Mapped[str | None] = mapped_column(UnicodeText())
