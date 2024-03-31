from datetime import datetime
from secrets import token_hex

from sqlalchemy import Column, DateTime, ForeignKey, String, Unicode
from sqlalchemy.orm import backref, relationship

from disco.models.meta import Base


class ApiKeyInvite(Base):
    __tablename__ = "api_key_invites"

    id = Column(String(32), default=lambda: token_hex(16), primary_key=True)
    created = Column(DateTime, default=datetime.utcnow)
    updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    name = Column(Unicode(255), nullable=False)
    expires = Column(DateTime, nullable=False)
    by_api_key_id = Column(
        String(32),
        ForeignKey("api_keys.id"),
        nullable=False,
        index=True,
    )
    api_key_id = Column(
        String(32),
        ForeignKey("api_keys.id"),
        nullable=True,
        index=True,
    )

    by_api_key = relationship(
        "ApiKey",
        foreign_keys=by_api_key_id,
        backref=backref("created_api_key_invites"),
    )
    api_key = relationship(
        "ApiKey",
        foreign_keys=api_key_id,
        backref=backref("from_invite"),
    )

    def log(self):
        return f"API_KEY_INVITE_{self.id} ({self.name})"
