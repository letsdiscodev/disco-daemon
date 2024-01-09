import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, String, Unicode

from disco.models.meta import Base


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String(32), default=lambda: uuid.uuid4().hex, primary_key=True)
    created = Column(DateTime, default=datetime.utcnow)
    updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    name = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False)
    body = Column(Unicode(10000), nullable=False)
    result = Column(Unicode(10000), nullable=True)

    def log(self):
        return f"TASK_{self.id}"
