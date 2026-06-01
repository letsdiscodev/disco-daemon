import logging

from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from disco.config import SQLALCHEMY_ASYNC_DATABASE_URL, SQLALCHEMY_DATABASE_URL

log = logging.getLogger(__name__)


def _enforce_query_only(dbapi_connection, connection_record):
    """Make every connection in a read pool reject writes.

    ``PRAGMA query_only=1`` is a connection-level setting: any INSERT/UPDATE/
    DELETE/DDL on the connection raises ``sqlite3.OperationalError: attempt to
    write a readonly database`` instead of silently succeeding. Read sessions
    are bound to dedicated read engines (separate connection pools), so a write
    connection never has this pragma set. This makes a mis-classified read
    session fail loudly the first time it tries to write, so the test suite
    catches it.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA query_only=1")
    cursor.close()


engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)


async_engine = create_async_engine(
    SQLALCHEMY_ASYNC_DATABASE_URL, connect_args={"check_same_thread": False}
)
AsyncSession = async_sessionmaker(
    autocommit=False, autoflush=False, bind=async_engine
)


# Dedicated read-only engines. Bound to the same database, but every connection
# is pinned to PRAGMA query_only=1 so accidental writes raise immediately. Use
# ReadSession / AsyncReadSession for transactions that only read.
read_engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
event.listen(read_engine, "connect", _enforce_query_only)
ReadSession = sessionmaker(autocommit=False, autoflush=False, bind=read_engine)


async_read_engine = create_async_engine(
    SQLALCHEMY_ASYNC_DATABASE_URL, connect_args={"check_same_thread": False}
)
event.listen(async_read_engine.sync_engine, "connect", _enforce_query_only)
AsyncReadSession = async_sessionmaker(
    autocommit=False, autoflush=False, bind=async_read_engine
)
