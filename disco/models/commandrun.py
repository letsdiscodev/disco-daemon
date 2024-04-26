import uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Unicode,
    UnicodeText,
)
from sqlalchemy.orm import relationship

from disco.models.meta import Base


class CommandRun(Base):
    __tablename__ = "command_runs"

    id = Column(String(32), default=lambda: uuid.uuid4().hex, primary_key=True)
    created = Column(DateTime, default=datetime.utcnow)
    updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    number = Column(Integer, nullable=False, index=True)
    service = Column(Unicode(), nullable=False)
    command = Column(UnicodeText(), nullable=False)
    status = Column(String(32), nullable=False)
    project_id = Column(
        String(32),
        ForeignKey("projects.id"),
        nullable=False,
        index=True,
    )
    deployment_id = Column(
        String(32),
        ForeignKey("deployments.id"),
        nullable=True,
        index=True,
    )
    by_api_key_id = Column(
        String(32),
        ForeignKey("api_keys.id"),
        nullable=False,
        index=True,
    )

    project = relationship(
        "Project",
        back_populates="command_runs",
    )
    by_api_key = relationship(
        "ApiKey",
        back_populates="command_runs",
    )
    deployment = relationship(
        "Deployment",
        back_populates="command_runs",
    )

    def log(self):
        return f"COMMAND_RUN_{self.id} ({self.project_name} {self.number})"
