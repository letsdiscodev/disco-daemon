from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models import CommandOutput


async def save(dbsession: AsyncDBSession, source: str, text: str | None) -> None:
    cmd_output = CommandOutput(
        source=source,
        text=text,
    )
    dbsession.add(cmd_output)


def save_sync(dbsession: DBSession, source: str, text: str | None) -> None:
    cmd_output = CommandOutput(
        source=source,
        text=text,
    )
    dbsession.add(cmd_output)


async def get_next(
    dbsession: AsyncDBSession, source: str, after: datetime | None = None
) -> CommandOutput | None:
    stmt = select(CommandOutput).where(CommandOutput.source == source)
    if after is not None:
        stmt = stmt.where(CommandOutput.created > after)
    stmt = stmt.order_by(CommandOutput.created).limit(1)
    result = await dbsession.execute(stmt)
    return result.scalars().first()


def delete_output_for_source(dbsession: DBSession, source: str) -> None:
    dbsession.query(CommandOutput).filter(CommandOutput.source == source).delete()


def get_by_id(dbsession, output_id) -> CommandOutput | None:
    return dbsession.query(CommandOutput).get(output_id)
