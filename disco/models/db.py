import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from disco import config
from disco.utils.dqlite import resolve_node_name

log = logging.getLogger(__name__)

_SQLITE_CONNECT_ARGS = {"check_same_thread": False}
_DQLITE_ASYNC_WRITE_ARGS = {"session_mode": "immediate"}
_DQLITE_ASYNC_READ_ARGS = {"session_mode": "read_only"}

_async_engine: AsyncEngine | None = None
_async_read_engine: AsyncEngine | None = None
_AsyncSession: async_sessionmaker | None = None
_AsyncReadSession: async_sessionmaker | None = None


def _enforce_query_only(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA query_only=1")
    cursor.close()


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


def get_async_session_factory() -> async_sessionmaker:
    global _AsyncSession
    if _AsyncSession is None:
        _AsyncSession = async_sessionmaker(
            autocommit=False, autoflush=False, bind=get_async_engine()
        )
    return _AsyncSession


def get_async_read_session_factory() -> async_sessionmaker:
    global _AsyncReadSession
    if _AsyncReadSession is None:
        _AsyncReadSession = async_sessionmaker(
            autocommit=False, autoflush=False, bind=get_async_read_engine()
        )
    return _AsyncReadSession


async def build_engines() -> None:
    if config.is_ha():
        await resolve_node_name()
    get_async_engine()
    get_async_read_engine()


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


AsyncSession = _LazySessionMaker(get_async_session_factory)
AsyncReadSession = _LazySessionMaker(get_async_read_session_factory)
