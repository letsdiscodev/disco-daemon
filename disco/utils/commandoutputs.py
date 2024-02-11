from datetime import datetime

from sqlalchemy.orm.session import Session as DBSession

from disco.models import CommandOutput


def save(dbsession: DBSession, source: str, text: str) -> None:
    cmd_output = CommandOutput(
        source=source,
        text=text,
    )
    dbsession.add(cmd_output)


def get_next(
    dbsession: DBSession, source: str, after: datetime | None = None
) -> CommandOutput | None:
    query = dbsession.query(CommandOutput).filter(CommandOutput.source == source)
    if after is not None:
        query = query.filter(CommandOutput.created > after)
    return query.order_by(CommandOutput.created).first()


def delete_output_for_source(dbsession: DBSession, source: str) -> None:
    dbsession.query(CommandOutput).filter(CommandOutput.source == source).delete()
