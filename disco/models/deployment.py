import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Unicode
from sqlalchemy.orm import backref, relationship

from disco.models.meta import Base


class Deployment(Base):
    __tablename__ = "deployments"

    id = Column(String(32), default=lambda: uuid.uuid4().hex, primary_key=True)
    created = Column(DateTime, default=datetime.utcnow)
    updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    number = Column(Integer, nullable=False, index=True)
    status = Column(String(32), nullable=False)
    commit_hash = Column(String(200), nullable=True)
    disco_file = Column(Unicode(5000), nullable=True)
    project_name = Column(Unicode(255), nullable=False)
    github_repo = Column(Unicode(2048), nullable=True)
    github_host = Column(Unicode(2048), nullable=True)
    domain = Column(Unicode(255), nullable=True)
    project_id = Column(
        String(32),
        ForeignKey("projects.id"),
        nullable=False,
        index=True,
    )
    prev_deployment_id = Column(
        String(32),
        ForeignKey("deployments.id"),
        nullable=True,
        index=True,
    )
    by_api_key_id = Column(
        String(32),
        ForeignKey("api_keys.id"),
        nullable=True,
        index=True,
    )

    project = relationship(
        "Project",
        foreign_keys=project_id,
        backref=backref("deployments", order_by="Deployment.number.desc()"),
    )
    by_api_key = relationship(
        "ApiKey",
        foreign_keys=by_api_key_id,
        backref=backref("deployments"),
    )
    prev_deployment = relationship(
        "Deployment",
        foreign_keys=prev_deployment_id,
    )

    def log(self):
        return f"DEPLOYMENT_{self.id} ({self.project_name} {self.number})"
