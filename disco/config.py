CADDY_VERSION = "2.9.1"
CADDY_IMAGE_TAG = "0.1.0"
DISCO_TUNNEL_VERSION = "1.0.0"
BUSYBOX_VERSION = "1.37.0"


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
