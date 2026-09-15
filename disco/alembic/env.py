import logging

from alembic import context
from alembic.ddl.sqlite import SQLiteImpl
from sqlalchemy import create_engine

from disco.config import get_database_url
from disco.models.meta import Base, DateTimeTzAware


class DqliteImpl(SQLiteImpl):
    __dialect__ = "dqlite"


config = context.config

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    if type_ == "type" and isinstance(obj, DateTimeTzAware):
        return "sa.DateTime()"
    # default rendering for other objects
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_item,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    logging.basicConfig(level=logging.INFO)

    connection = config.attributes.get("connection")
    if connection is None:
        # for alembic command line
        # init/update scripts pass their own connection
        engine = create_engine(config.get_main_option("sqlalchemy.url"))
        with engine.connect() as connection:
            _run_migrations(connection)
        return
    _run_migrations(connection)


def _run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_item=render_item,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
