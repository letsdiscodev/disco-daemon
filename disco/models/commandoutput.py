import uuid
from datetime import datetime, timezone

from sqlalchemy import String, UnicodeText
from sqlalchemy.orm import Mapped, mapped_column

from disco.models.meta import Base, DateTimeTzAware


class CommandOutput(Base):
    __tablename__ = "command_outputs"

    id: Mapped[str] = mapped_column(
        String(32), default=lambda: uuid.uuid4().hex, primary_key=True
    )
    created: Mapped[datetime] = mapped_column(
        DateTimeTzAware(),
        default=lambda: datetime.now(timezone.utc),
        index=True,
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    text: Mapped[str | None] = mapped_column(
        UnicodeText()
    )  # None means no more content
