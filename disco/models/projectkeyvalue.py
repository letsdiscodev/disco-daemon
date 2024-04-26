from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, UnicodeText
from sqlalchemy.orm import relationship

from disco.models.meta import Base


class ProjectKeyValue(Base):
    __tablename__ = "project_key_values"

    key = Column(String(255), primary_key=True)
    project_id = Column(
        String(32),
        ForeignKey("projects.id"),
        primary_key=True,
    )
    created = Column(DateTime, default=datetime.utcnow)
    updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    value = Column(UnicodeText())

    project = relationship(
        "Project",
        back_populates="key_values",
    )

    def log(self):
        return f"PROJECT_KEY_VAL_{self.key} ({self.project.name})"
