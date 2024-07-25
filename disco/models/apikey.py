from __future__ import annotations

from datetime import datetime, timezone
from secrets import token_hex
from typing import TYPE_CHECKING

from sqlalchemy import String, Unicode
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from disco.models import (
        ApiKeyInvite,
        ApiKeyUsage,
        CommandRun,
        CorsOrigin,
        Deployment,
        ProjectEnvironmentVariable,
    )

from disco.models.meta import Base, DateTimeTzAware


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(
        String(32), default=lambda: token_hex(16), primary_key=True
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
    name: Mapped[str] = mapped_column(Unicode(255), nullable=False)
    public_key: Mapped[str] = mapped_column(
        String(32), default=lambda: token_hex(16), nullable=False, index=True
    )
    deleted: Mapped[datetime | None] = mapped_column(DateTimeTzAware())

    created_api_key_invites: Mapped[list[ApiKeyInvite]] = relationship(
        "ApiKeyInvite",
        foreign_keys="ApiKeyInvite.by_api_key_id",
        back_populates="by_api_key",
    )
    created_cors_origins: Mapped[list[CorsOrigin]] = relationship(
        "CorsOrigin",
        foreign_keys="CorsOrigin.by_api_key_id",
        back_populates="by_api_key",
    )
    from_invite: Mapped[ApiKeyInvite | None] = relationship(
        "ApiKeyInvite", foreign_keys="ApiKeyInvite.api_key_id", back_populates="api_key"
    )
    command_runs: Mapped[list[CommandRun]] = relationship(
        "CommandRun", back_populates="by_api_key", order_by="CommandRun.number.desc()"
    )
    deployments: Mapped[list[Deployment]] = relationship(
        "Deployment", order_by="Deployment.number.desc()"
    )
    env_variables: Mapped[list[ProjectEnvironmentVariable]] = relationship(
        "ProjectEnvironmentVariable", back_populates="by_api_key"
    )
    usages: Mapped[list[ApiKeyUsage]] = relationship(
        "ApiKeyUsage", order_by="desc(ApiKeyUsage.created)", back_populates="api_key"
    )

    def log(self):
        return f"API_KEY_{self.public_key} ({self.name})"
