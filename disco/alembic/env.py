import logging

from alembic import context

from disco.models.meta import Base

config = context.config

target_metadata = Base.metadata


def run_migrations_online():
    logging.basicConfig(level=logging.INFO)

    from disco.models.db import engine

    connection = engine.connect()
    context.configure(connection=connection, target_metadata=target_metadata)
    try:
        with context.begin_transaction():
            context.run_migrations()
    finally:
        connection.close()


if context.is_offline_mode():
    raise NotImplementedError()
else:
    run_migrations_online()
