import logging

from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from disco.config import SQLALCHEMY_ASYNC_DATABASE_URL, SQLALCHEMY_DATABASE_URL

log = logging.getLogger(__name__)


def _enforce_query_only(dbapi_connection, connection_record):
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
AsyncSession = async_sessionmaker(autocommit=False, autoflush=False, bind=async_engine)


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
