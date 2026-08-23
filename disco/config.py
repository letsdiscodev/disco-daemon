import functools
import os
from typing import Literal

CADDY_VERSION = "2.9.1"
CADDY_IMAGE_TAG = "0.1.0"
DISCO_TUNNEL_VERSION = "1.0.0"
BUSYBOX_VERSION = "1.37.0"

SQLALCHEMY_DATABASE_URL = "sqlite:////disco/data/disco.sqlite3"
SQLALCHEMY_ASYNC_DATABASE_URL = "sqlite+aiosqlite:////disco/data/disco.sqlite3"

# Marker file that persists which mode this installation runs in.
# Lives on the disco-data volume, which is mounted by the daemon, the
# update container and the installer's init container. Absent file means
# sqlite: installations that predate the marker are all sqlite.
DISCO_MODE_FILE = "/disco/data/disco-mode"

DiscoMode = Literal["sqlite", "dqlite"]


@functools.cache
def get_disco_mode() -> DiscoMode:
    """Which storage/ingress stack this installation runs.

    Resolution order: DISCO_MODE env var (tests, one-shot containers that
    don't mount disco-data), then the marker file, then "sqlite".

    Cached per-process: a mode flip (init, migration) always goes through
    a daemon restart, never through a live process.
    """
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
    """Force this process's mode, regardless of marker file or import order.

    For one-shot containers that don't mount disco-data (dqlite tooling)
    and for rollback paths that must flip mode mid-process. Clears the
    get_disco_mode cache so the pin also wins if the mode was already
    resolved earlier in the process.
    """
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
    """Disco's custom Caddy image (stock Caddy + dqlite storage & config-sync).

    Overridable via the CADDY_IMAGE env var (used by the integration tester to
    point at an ephemeral registry).
    """
    import os

    return os.environ.get("CADDY_IMAGE", f"letsdiscodev/caddy:{CADDY_IMAGE_TAG}")


def get_dqlite_url() -> str:
    """Get dqlite connection URL.

    Must be called after node disco-name is available.

    """
    from disco.utils.dqlite import get_local_dqlite_address

    return f"dqlite://{get_local_dqlite_address()}/disco"


def get_dqlite_async_url() -> str:
    """Get async dqlite connection URL.

    Must be called after node disco-name is available.

    """
    from disco.utils.dqlite import get_local_dqlite_address

    return f"dqlite+aio://{get_local_dqlite_address()}/disco"
