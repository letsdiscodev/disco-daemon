import functools
import os
from typing import Literal

CADDY_VERSION = "2.9.1"
CADDY_IMAGE_TAG = "0.1.0"
DISCO_TUNNEL_VERSION = "1.0.0"
BUSYBOX_VERSION = "1.37.0"

SQLALCHEMY_DATABASE_URL = "sqlite:////disco/data/disco.sqlite3"
SQLALCHEMY_ASYNC_DATABASE_URL = "sqlite+aiosqlite:////disco/data/disco.sqlite3"

# On the disco-data volume. Missing file means sqlite (pre-marker installs).
DISCO_MODE_FILE = "/disco/data/disco-mode"

DiscoMode = Literal["sqlite", "dqlite"]


@functools.cache
def get_disco_mode() -> DiscoMode:
    mode = os.environ.get("DISCO_MODE")
    if mode is None:
        try:
            with open(DISCO_MODE_FILE, "r", encoding="utf-8") as f:
                mode = f.read().strip()
        except FileNotFoundError:
            mode = "sqlite"
    if mode == "dqlite":
        return "dqlite"
    if mode == "sqlite":
        return "sqlite"
    raise ValueError(f"Invalid disco mode: {mode!r}")


def is_dqlite_mode() -> bool:
    return get_disco_mode() == "dqlite"


def pin_mode(mode: DiscoMode) -> None:
    os.environ["DISCO_MODE"] = mode
    get_disco_mode.cache_clear()


def write_disco_mode(mode: DiscoMode) -> None:
    with open(DISCO_MODE_FILE, "w", encoding="utf-8") as f:
        f.write(mode)
    get_disco_mode.cache_clear()


def get_database_url() -> str:
    if is_dqlite_mode():
        return get_dqlite_url()
    return SQLALCHEMY_DATABASE_URL


def get_database_async_url() -> str:
    if is_dqlite_mode():
        return get_dqlite_async_url()
    return SQLALCHEMY_ASYNC_DATABASE_URL


def get_caddy_image() -> str:
    import os

    return os.environ.get("CADDY_IMAGE", f"letsdiscodev/caddy:{CADDY_IMAGE_TAG}")


def get_dqlite_url() -> str:
    from disco.utils.dqlite import get_local_dqlite_address

    return f"dqlite://{get_local_dqlite_address()}/disco"


def get_dqlite_async_url() -> str:
    from disco.utils.dqlite import get_local_dqlite_address

    return f"dqlite+aio://{get_local_dqlite_address()}/disco"
