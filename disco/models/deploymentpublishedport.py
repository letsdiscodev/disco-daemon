import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import backref, relationship

from disco.models.meta import Base


class DeploymentPublishedPort(Base):
    __tablename__ = "deployment_published_ports"

    id = Column(String(32), default=lambda: uuid.uuid4().hex, primary_key=True)
    created = Column(DateTime, default=datetime.utcnow)
    updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    host_port = Column(Integer, nullable=False)
    container_port = Column(Integer, nullable=False)
    protocol = Column(String(30), nullable=False)
    deployment_id = Column(
        String(32),
        ForeignKey("deployments.id"),
        nullable=False,
        index=True,
    )

    deployment = relationship(
        "Deployment",
        foreign_keys=deployment_id,
        backref=backref("published_ports"),
    )

    def log(self):
        return f"DEPLOY_VOLLUME_{self.deployment_id}_{self.name}"
