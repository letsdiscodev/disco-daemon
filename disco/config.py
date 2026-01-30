CADDY_VERSION = "2.9.1"
DISCO_TUNNEL_VERSION = "1.0.0"
BUSYBOX_VERSION = "1.37.0"


def get_dqlite_url() -> str:
    """Get dqlite connection URL. Must be called after node disco-name is available."""
    from disco.utils.dqlite import get_local_dqlite_address

    return f"dqlite://{get_local_dqlite_address()}/disco"


def get_dqlite_async_url() -> str:
    """Get async dqlite connection URL. Must be called after node disco-name is available."""
    from disco.utils.dqlite import get_local_dqlite_address

    return f"dqlite+aio://{get_local_dqlite_address()}/disco"
