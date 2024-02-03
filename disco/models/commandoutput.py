import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, String, UnicodeText

from disco.models.meta import Base


class CommandOutput(Base):
    __tablename__ = "command_outputs"

    id = Column(String(32), default=lambda: uuid.uuid4().hex, primary_key=True)
    created = Column(DateTime, default=datetime.utcnow, index=True)
    source = Column(String(100), nullable=False, index=True)
    text = Column(UnicodeText())  # None means no more content
