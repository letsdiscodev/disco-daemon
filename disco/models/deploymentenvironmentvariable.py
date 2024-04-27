from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Unicode
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from disco.models import Deployment

from disco.models.meta import Base, DateTimeTzAware


class DeploymentEnvironmentVariable(Base):
    __tablename__ = "deployment_env_variables"

    id: Mapped[str] = mapped_column(
        String(32), default=lambda: uuid.uuid4().hex, primary_key=True
    )
    created: Mapped[datetime] = mapped_column(
        DateTimeTzAware(),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated: Mapped[datetime] = mapped_column(
        DateTimeTzAware(),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(Unicode(4000), nullable=False)
    deployment_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("deployments.id"),
        nullable=False,
        index=True,
    )

    deployment: Mapped[Deployment] = relationship(
        "Deployment",
        back_populates="env_variables",
    )

    def log(self):
        return f"DEPLOY_ENV_VAR_{self.deployment_id}_{self.name}"
