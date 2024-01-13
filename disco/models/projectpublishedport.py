import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import backref, relationship

from disco.models.meta import Base


class ProjectPublishedPort(Base):
    __tablename__ = "project_published_ports"

    id = Column(String(32), default=lambda: uuid.uuid4().hex, primary_key=True)
    created = Column(DateTime, default=datetime.utcnow)
    updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    host_port = Column(Integer, nullable=False, index=True)
    container_port = Column(Integer, nullable=False)
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
        foreign_keys=project_id,
        backref=backref("published_ports"),
    )
    by_api_key = relationship(
        "ApiKey",
        foreign_keys=by_api_key_id,
    )

    def log(self):
        return (
            f"PROJECT_PUBLISHED_PORT_{self.project.name}_"
            f"{self.host_port}_{self.container_port}"
        )
