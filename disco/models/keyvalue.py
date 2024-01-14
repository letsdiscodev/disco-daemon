from datetime import datetime

from sqlalchemy import Column, DateTime, String, UnicodeText

from disco.models.meta import Base


class KeyValue(Base):
    __tablename__ = "key_values"

    key = Column(String(255), primary_key=True)
    created = Column(DateTime, default=datetime.utcnow)
    updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    value = Column(UnicodeText())
