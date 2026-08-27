import logging

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from disco import config

log = logging.getLogger(__name__)

_SQLITE_CONNECT_ARGS = {"check_same_thread": False}
_DQLITE_WRITE_ARGS = {"check_same_thread": False, "session_mode": "immediate"}
_DQLITE_READ_ARGS = {"check_same_thread": False, "session_mode": "read_only"}
_DQLITE_ASYNC_WRITE_ARGS = {"session_mode": "immediate"}
_DQLITE_ASYNC_READ_ARGS = {"session_mode": "read_only"}

_engine: Engine | None = None
_async_engine: AsyncEngine | None = None
_read_engine: Engine | None = None
_async_read_engine: AsyncEngine | None = None
_Session: sessionmaker | None = None
_AsyncSession: async_sessionmaker | None = None
_ReadSession: sessionmaker | None = None
_AsyncReadSession: async_sessionmaker | None = None


def _enforce_query_only(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA query_only=1")
    cursor.close()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            config.get_database_url(),
            connect_args=_DQLITE_WRITE_ARGS
            if config.is_ha()
            else _SQLITE_CONNECT_ARGS,
        )
    return _engine


def get_async_engine() -> AsyncEngine:
    global _async_engine
    if _async_engine is None:
        _async_engine = create_async_engine(
            config.get_database_async_url(),
            connect_args=_DQLITE_ASYNC_WRITE_ARGS
            if config.is_ha()
            else _SQLITE_CONNECT_ARGS,
        )
    return _async_engine


def get_read_engine() -> Engine:
    global _read_engine
    if _read_engine is None:
        _read_engine = create_engine(
            config.get_database_url(),
            connect_args=_DQLITE_READ_ARGS
            if config.is_ha()
            else _SQLITE_CONNECT_ARGS,
        )
        if not config.is_ha():
            event.listen(_read_engine, "connect", _enforce_query_only)
    return _read_engine


def get_async_read_engine() -> AsyncEngine:
    global _async_read_engine
    if _async_read_engine is None:
        _async_read_engine = create_async_engine(
            config.get_database_async_url(),
            connect_args=_DQLITE_ASYNC_READ_ARGS
            if config.is_ha()
            else _SQLITE_CONNECT_ARGS,
        )
        if not config.is_ha():
            event.listen(_async_read_engine.sync_engine, "connect", _enforce_query_only)
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
ReadSession = _LazySessionMaker(get_read_session_factory)
AsyncSession = _LazySessionMaker(get_async_session_factory)
AsyncReadSession = _LazySessionMaker(get_async_read_session_factory)
