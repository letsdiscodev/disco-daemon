import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Unicode
from sqlalchemy.orm import relationship

from disco.models.meta import Base


class ProjectEnvironmentVariable(Base):
    __tablename__ = "project_env_variables"

    id = Column(String(32), default=lambda: uuid.uuid4().hex, primary_key=True)
    created = Column(DateTime, default=datetime.utcnow)
    updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    name = Column(String(255), nullable=False, index=True)
    value = Column(Unicode(4000), nullable=False)
    project_id = Column(
        String(32),
        ForeignKey("projects.id"),
        nullable=False,
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
        back_populates="env_variables",
    )
    by_api_key = relationship(
        "ApiKey",
        back_populates="env_variables",
    )

    def log(self):
        return f"PROJECT_ENV_VAR_{self.name}"
