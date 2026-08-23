"""Read-only access to the dqlite Caddy config-sync state.

The Caddy config-sync module (``caddy-config-dqlite``) owns the ``caddy_config``
and ``caddy_instances`` tables in the ``disco`` dqlite database. Disco only
READS them, to gate deployment teardown on fleet-wide convergence: after Disco
edits the local Caddy over the admin socket (which the config-sync module then
publishes to dqlite), we wait until every live Caddy instance has applied the
new config version before removing the previous deployment's containers.

Disco never WRITES config to dqlite — that is owned by the config-sync module.
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from disco import config
from disco.models.db import AsyncReadSession

log = logging.getLogger(__name__)

# A Caddy instance counts as "live" if it refreshed its heartbeat within this
# window. Instances that went away (dead/rescheduled) must not freeze deploys.
INSTANCE_FRESH_SECONDS = 15


async def get_config_version() -> int | None:
    """Current ``caddy_config`` version, or None if the table/row is absent.

    Returns None when the config-sync tables don't exist yet (Caddy hasn't
    started) so callers can treat it as "nothing to wait for".
    """
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
    """The full Caddy config blob currently published in dqlite, or None.

    Used by the edit gate to tell whether the local Caddy is current before
    Disco edits it.
    """
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
    """Return (instance_id, applied_version) for instances with a recent heartbeat."""
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
    """Wait until our Caddy config edit has propagated to every live instance.

    ``after_version`` is the ``caddy_config`` version observed *before* the edit.
    We wait for (1) a newer version to appear — the config-sync module publishing
    our edit — and then (2) every live instance to apply at least that version.

    Returns True on convergence; False on timeout (the caller should proceed and
    log: a wedged/dead instance must not freeze deploys). Also returns False if
    no new version ever appears (e.g. the edit was a no-op), which is harmless.

    In sqlite mode there is a single Caddy and no config-sync tables — return
    immediately so a caller missing its own mode guard can't burn the timeout.
    """
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
