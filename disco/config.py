import functools
import os

CADDY_VERSION = "2.9.1"
DISCO_TUNNEL_VERSION = "1.0.0"
BUSYBOX_VERSION = "1.37.0"

SQLITE_PATH = "/disco/data/disco.sqlite3"
SQLALCHEMY_DATABASE_URL = f"sqlite:///{SQLITE_PATH}"
SQLALCHEMY_ASYNC_DATABASE_URL = f"sqlite+aiosqlite:///{SQLITE_PATH}"


@functools.cache
def is_ha() -> bool:
    return not os.path.exists(SQLITE_PATH)


def get_database_url() -> str:
    if is_ha():
        from disco.utils.dqlite import get_local_dqlite_address

        return f"dqlite://{get_local_dqlite_address()}/disco"
    return SQLALCHEMY_DATABASE_URL


def get_database_async_url() -> str:
    if is_ha():
        from disco.utils.dqlite import get_local_dqlite_address

        return f"dqlite+aio://{get_local_dqlite_address()}/disco"
    return SQLALCHEMY_ASYNC_DATABASE_URL
