import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from disco import config
from disco.utils.dqlite import resolve_node_name

log = logging.getLogger(__name__)

_SQLITE_CONNECT_ARGS = {"check_same_thread": False}
_DQLITE_WRITE_ARGS = {"session_mode": "immediate"}
_DQLITE_READ_ARGS = {"session_mode": "read_only"}

_engine: AsyncEngine | None = None
_read_engine: AsyncEngine | None = None
_Session: async_sessionmaker | None = None
_ReadSession: async_sessionmaker | None = None


def _enforce_query_only(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA query_only=1")
    cursor.close()


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            config.get_database_url(),
            connect_args=_DQLITE_WRITE_ARGS if config.is_ha() else _SQLITE_CONNECT_ARGS,
        )
    return _engine


def get_read_engine() -> AsyncEngine:
    global _read_engine
    if _read_engine is None:
        _read_engine = create_async_engine(
            config.get_database_url(),
            connect_args=_DQLITE_READ_ARGS if config.is_ha() else _SQLITE_CONNECT_ARGS,
        )
        if not config.is_ha():
            event.listen(_read_engine.sync_engine, "connect", _enforce_query_only)
    return _read_engine


def get_session_factory() -> async_sessionmaker:
    global _Session
    if _Session is None:
        _Session = async_sessionmaker(
            autocommit=False, autoflush=False, bind=get_engine()
        )
    return _Session


def get_read_session_factory() -> async_sessionmaker:
    global _ReadSession
    if _ReadSession is None:
        _ReadSession = async_sessionmaker(
            autocommit=False, autoflush=False, bind=get_read_engine()
        )
    return _ReadSession


async def build_engines() -> None:
    if config.is_ha():
        await resolve_node_name()
    get_engine()
    get_read_engine()


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
