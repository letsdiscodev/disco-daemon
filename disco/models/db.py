import logging
import random
import time
from typing import TYPE_CHECKING

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from disco.config import get_dqlite_async_url, get_dqlite_url

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)

# dqlite surfaces concurrent-write contention as SQLite's standard
# "database is locked" (SQLITE_BUSY). Stdlib sqlite3 quietly retries via
# its default busy_timeout; dqlite-dbapi 0.2.0 does not, and dqlite's
# server-side authorizer rejects `PRAGMA busy_timeout`. So we retry at
# the dialect's do_execute / do_executemany boundary: each statement
# attempt that fails with a "is locked" OperationalError waits and
# retries until the budget runs out. This is the SQLAlchemy-recommended
# pattern when the DBAPI doesn't honour busy_timeout itself.
_BUSY_RETRY_TOTAL_SECONDS = 30.0
_BUSY_RETRY_INITIAL_BACKOFF = 0.05
_BUSY_RETRY_MAX_BACKOFF = 1.0


def _is_busy(exc: BaseException) -> bool:
    # The message format is "database is locked to <leader-address>"
    # (see dqliteclient/connection.py:_RAFT_BUSY_MESSAGE_FRAGMENTS). The
    # surrounding wrapper is an OperationalError; matching on substring
    # is enough — dqlite never produces this string for any non-BUSY case.
    return "is locked" in str(exc)


def _install_busy_retry(dialect) -> None:
    """Wrap the dialect's do_execute / do_executemany with a BUSY retry loop.

    Statement-level (not transaction-level) retry: if a write fails because
    another writer holds the lock, we wait and re-execute the same statement
    against the same cursor. The cursor's existing transaction context is
    untouched.
    """
    from dqlitedbapi.exceptions import OperationalError as DqliteOperationalError

    original_do_execute = dialect.do_execute
    original_do_executemany = dialect.do_executemany

    def _retry(fn, *args, **kwargs):
        deadline = time.monotonic() + _BUSY_RETRY_TOTAL_SECONDS
        backoff = _BUSY_RETRY_INITIAL_BACKOFF
        while True:
            try:
                return fn(*args, **kwargs)
            except DqliteOperationalError as exc:
                if not _is_busy(exc) or time.monotonic() >= deadline:
                    raise
                # Jitter to spread retry storms when several callers race.
                time.sleep(backoff * (0.5 + random.random()))
                backoff = min(backoff * 2, _BUSY_RETRY_MAX_BACKOFF)

    def do_execute(cursor, statement, parameters, context=None):
        return _retry(original_do_execute, cursor, statement, parameters, context)

    def do_executemany(cursor, statement, parameters, context=None):
        return _retry(original_do_executemany, cursor, statement, parameters, context)

    dialect.do_execute = do_execute
    dialect.do_executemany = do_executemany

_engine: "Engine | None" = None
_async_engine: "AsyncEngine | None" = None
_Session: sessionmaker | None = None
_AsyncSession: async_sessionmaker | None = None


def get_engine() -> "Engine":
    global _engine
    if _engine is None:
        # dqlite-dbapi 0.2.0 enforces a stdlib-sqlite3-style thread-affinity
        # rule: a Connection can only be used from the thread that created
        # it, and there's no `check_same_thread=False` opt-out. FastAPI runs
        # sync deps on a threadpool, so a pooled connection that was opened
        # by one worker thread can't safely be checked out by another.
        # NullPool sidesteps this by opening a fresh dqlite connection at
        # every checkout and closing it at release — so the creator thread
        # is always the caller thread. The async engine doesn't need this
        # because it stays on the event-loop thread.
        _engine = create_engine(get_dqlite_url(), poolclass=NullPool)
        _install_busy_retry(_engine.dialect)
    return _engine


def get_async_engine() -> "AsyncEngine":
    global _async_engine
    if _async_engine is None:
        _async_engine = create_async_engine(get_dqlite_async_url())
        _install_busy_retry(_async_engine.sync_engine.dialect)
    return _async_engine


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


class _LazyEngine:
    def __init__(self, engine_getter):
        self._engine_getter = engine_getter
        self._engine = None

    def __getattr__(self, name):
        if self._engine is None:
            self._engine = self._engine_getter()
        return getattr(self._engine, name)


engine = _LazyEngine(get_engine)
