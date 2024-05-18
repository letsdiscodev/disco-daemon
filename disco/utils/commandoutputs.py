import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import String, UnicodeText, select
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import MetaData

from disco.models.meta import NAMING_CONVENTION, DateTimeTzAware

log = logging.getLogger(__name__)

base_metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(AsyncAttrs, DeclarativeBase):
    metadata = base_metadata


class CommandOutput(Base):
    __tablename__ = "command_outputs"

    id: Mapped[str] = mapped_column(
        String(32), default=lambda: uuid.uuid4().hex, primary_key=True
    )
    created: Mapped[datetime] = mapped_column(
        DateTimeTzAware(),
        default=lambda: datetime.now(timezone.utc),
        index=True,
        nullable=False,
    )
    # None means no more content
    text: Mapped[str | None] = mapped_column(UnicodeText())


@dataclass
class Output:
    id: str
    created: datetime
    text: str | None


@dataclass
class OutputDbConnection:
    last_used: datetime
    engine: AsyncEngine
    session: async_sessionmaker[AsyncDBSession]


_dbs_lock = asyncio.Lock()  # when adding/removing dbs
_dbs: dict[str, OutputDbConnection] = {}


def _db_url(source: str) -> str:
    return f"sqlite+aiosqlite:///{_db_file_path(source)}"


def _db_file_path(source: str) -> str:
    return f"/disco/data/commandoutputs/{source}.sqlite3"


async def _db_connection(source: str) -> OutputDbConnection:
    global _dbs
    if source not in _dbs:
        async with _dbs_lock:
            if source not in _dbs:  # double check now that we have the lock
                engine = create_async_engine(
                    _db_url(source), connect_args={"check_same_thread": False}
                )
                session = async_sessionmaker(
                    autocommit=False, autoflush=False, bind=engine
                )
                _dbs[source] = OutputDbConnection(
                    engine=engine,
                    session=session,
                    last_used=datetime.now(timezone.utc),
                )
    _dbs[source].last_used = datetime.now(timezone.utc)
    return _dbs[source]


async def init(source: str) -> None:
    directory = "/disco/data/commandoutputs"
    if not os.path.isdir(directory):
        os.makedirs(directory)
    engine = (await _db_connection(source)).engine
    async with engine.begin() as conn:
        await conn.run_sync(base_metadata.create_all)


async def _dispose(source: str) -> None:
    async with _dbs_lock:
        if source in _dbs:
            log.info("Disposing of DB connection for command output %s", source)
            await _dbs[source].engine.dispose()
            del _dbs[source]


async def store_output(source: str, text: str) -> None:
    AsyncSession = (await _db_connection(source)).session
    async with AsyncSession.begin() as dbsession:
        _log(dbsession=dbsession, source=source, text=text)


async def terminate(source: str) -> None:
    AsyncSession = (await _db_connection(source)).session
    async with AsyncSession.begin() as dbsession:
        _log(dbsession=dbsession, source=source, text=None)


def _log(dbsession: AsyncDBSession, source: str, text: str | None) -> None:
    cmd_output = CommandOutput(
        text=text,
    )
    dbsession.add(cmd_output)


async def get_next(source: str, after: datetime | None = None) -> Output | None:
    AsyncSession = (await _db_connection(source)).session
    async with AsyncSession.begin() as dbsession:
        stmt = select(CommandOutput)
        if after is not None:
            stmt = stmt.where(CommandOutput.created > after)
        stmt = stmt.order_by(CommandOutput.created).limit(1)
        result = await dbsession.execute(stmt)
        cmd_output = result.scalars().first()
        if cmd_output is None:
            return None
        return Output(
            id=cmd_output.id, created=cmd_output.created, text=cmd_output.text
        )


def delete_output_for_source(source: str) -> None:
    f = Path(_db_file_path(source))
    f.unlink(missing_ok=True)


async def get_by_id(source: str, output_id: str) -> Output | None:
    AsyncSession = (await _db_connection(source)).session
    async with AsyncSession.begin() as dbsession:
        cmd_output = await dbsession.get(CommandOutput, output_id)
        if cmd_output is None:
            return None
        return Output(
            id=cmd_output.id,
            created=cmd_output.created,
            text=cmd_output.text,
        )


async def clean_up_db_connections() -> None:
    global _dbs
    six_hours_ago = datetime.now(timezone.utc) - timedelta(hours=6)
    old_db_sources = set()
    for source, db in _dbs.items():
        if db.last_used < six_hours_ago:
            old_db_sources.add(source)
    for source in old_db_sources:
        await _dispose(source)


def deployment_source(deployment_id: str) -> str:
    return f"deployment_{deployment_id}"


def run_source(run_id: str) -> str:
    return f"run_{run_id}"
