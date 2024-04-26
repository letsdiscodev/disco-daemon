from datetime import datetime
from secrets import token_hex

from sqlalchemy import Column, DateTime, String, Unicode
from sqlalchemy.orm import relationship

from disco.models.meta import Base


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(String(32), default=lambda: token_hex(16), primary_key=True)
    created = Column(DateTime, default=datetime.utcnow)
    updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    name = Column(Unicode(255), nullable=False)
    public_key = Column(
        String(32), default=lambda: token_hex(16), nullable=False, index=True
    )
    deleted = Column(DateTime)

    created_api_key_invites = relationship(
        "ApiKeyInvite",
        foreign_keys="ApiKeyInvite.by_api_key_id",
        back_populates="by_api_key",
    )
    from_invite = relationship(
        "ApiKeyInvite", foreign_keys="ApiKeyInvite.api_key_id", back_populates="api_key"
    )
    command_runs = relationship(
        "CommandRun", back_populates="by_api_key", order_by="CommandRun.number.desc()"
    )
    deployments = relationship("Deployment", order_by="Deployment.number.desc()")
    env_variables = relationship(
        "ProjectEnvironmentVariable", back_populates="by_api_key"
    )

    def log(self):
        return f"API_KEY_{self.public_key} ({self.name})"
