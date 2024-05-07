import logging

from alembic import context

from disco.config import SQLALCHEMY_DATABASE_URL
from disco.models.meta import Base, DateTimeTzAware

config = context.config

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    if type_ == "type" and isinstance(obj, DateTimeTzAware):
        return "sa.DateTime()"
    # default rendering for other objects
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=SQLALCHEMY_DATABASE_URL,
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

    from disco.models.db import engine

    connection = engine.connect()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_item=render_item,
        render_as_batch=True,
    )
    try:
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.close()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
