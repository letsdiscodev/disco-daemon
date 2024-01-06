from datetime import datetime
from secrets import token_hex

from sqlalchemy import Column, DateTime, String, Unicode

from disco.models.meta import Base


class AuthenticationToken(Base):
    __tablename__ = "auth_tokens"

    id = Column(String(32), default=lambda: token_hex(16), primary_key=True)
    created = Column(DateTime, default=datetime.utcnow)
    updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    name = Column(Unicode(255), nullable=False)
    # to show entries in the logs without leaking credentials
    log_id = Column(String(32), default=lambda: token_hex(16), nullable=False)

    def log(self):
        return "AUTH_TOKEN_{log_id}".format(log_id=self.log_id)
