import logging
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from disco import config

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)

# dqlite-dbapi session_mode: "immediate" rewrites BEGIN to BEGIN IMMEDIATE so
# writers take the write lock up front; "read_only" sets PRAGMA query_only=1.
# check_same_thread only exists on the sync connection; the async one rejects it.
_WRITE_SYNC_CONNECT_ARGS = {
    "check_same_thread": False,
    "session_mode": "immediate",
}
_READ_SYNC_CONNECT_ARGS = {
    "check_same_thread": False,
    "session_mode": "read_only",
}
_WRITE_ASYNC_CONNECT_ARGS = {"session_mode": "immediate"}
_READ_ASYNC_CONNECT_ARGS = {"session_mode": "read_only"}

# sqlite mode gets the same behavior via engine events below. timeout is 30s
# because the hourly backup holds the write lock for the whole copy.
_SQLITE_CONNECT_ARGS = {"check_same_thread": False, "timeout": 30}


def _enforce_query_only(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA query_only=1")
    cursor.close()


def _sqlite_connect_autocommit(dbapi_connection, connection_record):
    dbapi_connection.isolation_level = None


def _sqlite_begin_immediate(conn):
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def _listen_sqlite_write(sync_engine) -> None:
    event.listen(sync_engine, "connect", _sqlite_connect_autocommit)
    event.listen(sync_engine, "begin", _sqlite_begin_immediate)

_engine: "Engine | None" = None
_async_engine: "AsyncEngine | None" = None
_read_engine: "Engine | None" = None
_async_read_engine: "AsyncEngine | None" = None
_Session: sessionmaker | None = None
_AsyncSession: async_sessionmaker | None = None
_ReadSession: sessionmaker | None = None
_AsyncReadSession: async_sessionmaker | None = None


def get_engine() -> "Engine":
    global _engine
    if _engine is None:
        if config.is_dqlite_mode():
            _engine = create_engine(
                config.get_database_url(), connect_args=_WRITE_SYNC_CONNECT_ARGS
            )
        else:
            _engine = create_engine(
                config.get_database_url(), connect_args=_SQLITE_CONNECT_ARGS
            )
            _listen_sqlite_write(_engine)
    return _engine


def get_async_engine() -> "AsyncEngine":
    global _async_engine
    if _async_engine is None:
        if config.is_dqlite_mode():
            _async_engine = create_async_engine(
                config.get_database_async_url(),
                connect_args=_WRITE_ASYNC_CONNECT_ARGS,
            )
        else:
            _async_engine = create_async_engine(
                config.get_database_async_url(), connect_args=_SQLITE_CONNECT_ARGS
            )
            _listen_sqlite_write(_async_engine.sync_engine)
    return _async_engine


def get_read_engine() -> "Engine":
    global _read_engine
    if _read_engine is None:
        if config.is_dqlite_mode():
            _read_engine = create_engine(
                config.get_database_url(), connect_args=_READ_SYNC_CONNECT_ARGS
            )
        else:
            _read_engine = create_engine(
                config.get_database_url(), connect_args=_SQLITE_CONNECT_ARGS
            )
            event.listen(_read_engine, "connect", _enforce_query_only)
    return _read_engine


def get_async_read_engine() -> "AsyncEngine":
    global _async_read_engine
    if _async_read_engine is None:
        if config.is_dqlite_mode():
            _async_read_engine = create_async_engine(
                config.get_database_async_url(),
                connect_args=_READ_ASYNC_CONNECT_ARGS,
            )
        else:
            _async_read_engine = create_async_engine(
                config.get_database_async_url(), connect_args=_SQLITE_CONNECT_ARGS
            )
            event.listen(
                _async_read_engine.sync_engine, "connect", _enforce_query_only
            )
    return _async_read_engine


def get_session_factory() -> sessionmaker:
    global _Session
    if _Session is None:
        _Session = sessionmaker(autocommit=False, autoflush=False, bind=get_engine())
    return _Session


def get_async_session_factory() -> async_sessionmaker:
    global _AsyncSession
    if _AsyncSession is None:
        _AsyncSession = async_sessionmaker(
            autocommit=False, autoflush=False, bind=get_async_engine()
        )
    return _AsyncSession


def get_read_session_factory() -> sessionmaker:
    global _ReadSession
    if _ReadSession is None:
        _ReadSession = sessionmaker(
            autocommit=False, autoflush=False, bind=get_read_engine()
        )
    return _ReadSession


def get_async_read_session_factory() -> async_sessionmaker:
    global _AsyncReadSession
    if _AsyncReadSession is None:
        _AsyncReadSession = async_sessionmaker(
            autocommit=False, autoflush=False, bind=get_async_read_engine()
        )
    return _AsyncReadSession


class _LazySessionMaker:

    def __init__(self, factory_getter):
        self._factory_getter = factory_getter
        self._factory = None

    def __call__(self, *args, **kwargs):
        if self._factory is None:
            self._factory = self._factory_getter()
        return self._factory(*args, **kwargs)

    def begin(self):
        if self._factory is None:
            self._factory = self._factory_getter()
        return self._factory.begin()


Session = _LazySessionMaker(get_session_factory)
AsyncSession = _LazySessionMaker(get_async_session_factory)
ReadSession = _LazySessionMaker(get_read_session_factory)
AsyncReadSession = _LazySessionMaker(get_async_read_session_factory)


class _LazyEngine:
    def __init__(self, engine_getter):
        self._engine_getter = engine_getter
        self._engine = None

    def __getattr__(self, name):
        if self._engine is None:
            self._engine = self._engine_getter()
        return getattr(self._engine, name)


engine = _LazyEngine(get_engine)
read_engine = _LazyEngine(get_read_engine)
