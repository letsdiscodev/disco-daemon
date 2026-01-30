import logging
from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from disco.config import get_dqlite_async_url, get_dqlite_url

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)

# Deferred engine creation - engines are created on first access
# because the node's disco-name may not be available at import time
_engine: "Engine | None" = None
_async_engine: "AsyncEngine | None" = None
_Session: sessionmaker | None = None
_AsyncSession: async_sessionmaker | None = None


def get_engine() -> "Engine":
    """Get the SQLAlchemy engine, creating it if necessary."""
    global _engine
    if _engine is None:
        _engine = create_engine(get_dqlite_url())
    return _engine


def get_async_engine() -> "AsyncEngine":
    """Get the async SQLAlchemy engine, creating it if necessary."""
    global _async_engine
    if _async_engine is None:
        _async_engine = create_async_engine(get_dqlite_async_url())
    return _async_engine


def get_session_factory() -> sessionmaker:
    """Get the session factory, creating it if necessary."""
    global _Session
    if _Session is None:
        _Session = sessionmaker(autocommit=False, autoflush=False, bind=get_engine())
    return _Session


def get_async_session_factory() -> async_sessionmaker:
    """Get the async session factory, creating it if necessary."""
    global _AsyncSession
    if _AsyncSession is None:
        _AsyncSession = async_sessionmaker(
            autocommit=False, autoflush=False, bind=get_async_engine()
        )
    return _AsyncSession


# For backward compatibility, provide Session and AsyncSession as properties
# that lazily initialize on first access
class _LazySessionMaker:
    """Lazy session maker that initializes the engine on first use."""

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


# For backward compatibility, provide engine as a lazy property
class _LazyEngine:
    """Lazy engine that initializes on first attribute access."""

    def __init__(self, engine_getter):
        self._engine_getter = engine_getter
        self._engine = None

    def __getattr__(self, name):
        if self._engine is None:
            self._engine = self._engine_getter()
        return getattr(self._engine, name)


engine = _LazyEngine(get_engine)
