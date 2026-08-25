# caddy_config and caddy_instances are owned by the caddy-config-dqlite Caddy
# module. Disco only reads them; never write to them from here.

import asyncio
import logging
import time
from typing import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from disco import config
from disco.models.db import AsyncReadSession

log = logging.getLogger(__name__)

# Instances without a heartbeat in this window are ignored so dead ones
# don't block deploys.
INSTANCE_FRESH_SECONDS = 15


async def get_config_version() -> int | None:
    if not config.is_dqlite_mode():
        return None
    try:
        async with AsyncReadSession() as dbsession:
            result = await dbsession.execute(
                text("SELECT version FROM caddy_config WHERE id = 1")
            )
            row = result.first()
        return int(row[0]) if row is not None else None
    except OperationalError:
        return None


async def get_config_bytes() -> bytes | None:
    if not config.is_dqlite_mode():
        return None
    try:
        async with AsyncReadSession() as dbsession:
            result = await dbsession.execute(
                text("SELECT config FROM caddy_config WHERE id = 1")
            )
            row = result.first()
        if row is None:
            return None
        value = row[0]
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        return str(value).encode()
    except OperationalError:
        return None


async def live_instances(
    fresh_seconds: int = INSTANCE_FRESH_SECONDS,
) -> list[tuple[str, int]]:
    cutoff = time.time_ns() - fresh_seconds * 1_000_000_000
    try:
        async with AsyncReadSession() as dbsession:
            result = await dbsession.execute(
                text(
                    "SELECT instance_id, applied_version FROM caddy_instances "
                    "WHERE updated_at >= :cutoff"
                ),
                {"cutoff": cutoff},
            )
            rows = result.all()
        return [(str(r[0]), int(r[1])) for r in rows]
    except OperationalError:
        return []


async def wait_for_convergence(
    after_version: int | None,
    timeout: float = 30.0,
    poll: float = 0.5,
    log_output: Callable[[str], Awaitable[None]] | None = None,
) -> bool:
    """Returns False on timeout; callers proceed anyway."""
    if not config.is_dqlite_mode():
        return True
    deadline = time.monotonic() + timeout

    target: int | None = None
    while time.monotonic() < deadline:
        version = await get_config_version()
        if version is not None and (after_version is None or version > after_version):
            target = version
            break
        await asyncio.sleep(poll)
    if target is None:
        return False

    while time.monotonic() < deadline:
        instances = await live_instances()
        if instances and all(version >= target for _, version in instances):
            if log_output is not None:
                await log_output(
                    f"Caddy config v{target} applied by all "
                    f"{len(instances)} instance(s)\n"
                )
            return True
        await asyncio.sleep(poll)

    behind = [iid for iid, version in await live_instances() if version < target]
    if log_output is not None:
        await log_output(
            f"Timed out waiting for Caddy config v{target} to converge; "
            f"proceeding (instances behind: {behind or 'unknown'})\n"
        )
    log.warning("Caddy convergence timeout at version %s (behind: %s)", target, behind)
    return False
