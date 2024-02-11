import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, String, Unicode

from disco.models.meta import Base


class Project(Base):
    __tablename__ = "projects"

    id = Column(String(32), default=lambda: uuid.uuid4().hex, primary_key=True)
    created = Column(DateTime, default=datetime.utcnow)
    updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    name = Column(Unicode(255), nullable=False)
    domain = Column(Unicode(255), nullable=True)
    github_repo = Column(Unicode(2048), nullable=True)
    github_webhook_token = Column(String(32), nullable=True)
    github_host = Column(Unicode(2048), nullable=True)

    def log(self):
        return f"PROJECT_{self.id} ({self.name})"
