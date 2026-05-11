"""Docker network clean-up.

We're doing it as part of a daily cron because of
https://github.com/moby/moby/issues/37338

The way we understand the issue is that we need to make ultra sure that there are
no services/containers still attached to the network, and also that the raft data
store had to time to catch up.

The workaround is to not remove networks as soon as they're not used
(commit 6069879007d8be290daee34ed10c5356f0d0abaf). And then, a few days later, delete
the network. The delay is to let the "docker service rm" command to take full effect
(the call just tells Docker Swarm to start getting rid of the service, but the actual
effect is async) and to let the raft data propagate.

This daily cron checks to make sure a network is not used, then wait 24 hours to check
again and delete it if the network is really not used.

We do that for project networks we create for each deployment.

"""

import logging
from datetime import datetime, timedelta, timezone

from disco.models.db import AsyncSession
from disco.utils import docker, keyvalues
from disco.utils.imagecleanup import get_active_projects

log = logging.getLogger(__name__)

EMPTY_SINCE_KEY_PREFIX = "network_empty_since:"
SETTLE_DURATION = timedelta(hours=24)
ORPHAN_DURATION = timedelta(days=7)


async def remove_unused_networks() -> None:
    log.info("Cleaning up Docker project networks")
    network_names = await docker.list_project_networks()
    active_projects = await get_active_projects()
    active_network_names = {
        docker.deployment_network_name(p.project_name, p.deployment_number)
        for p in active_projects
    }
    now = datetime.now(timezone.utc)
    for name in network_names:
        key = f"{EMPTY_SINCE_KEY_PREFIX}{name}"
        async with AsyncSession.begin() as dbsession:
            if name in active_network_names:
                existing = await keyvalues.get_value(dbsession, key)
                if existing is not None:
                    await keyvalues.delete_value(dbsession, key)
                continue
        try:
            info = await docker.inspect_network(name)
        except Exception:
            log.error("Failed to inspect network %s, skipping", name)
            continue
        if not _network_is_empty(info):
            continue
        async with AsyncSession.begin() as dbsession:
            empty_since_str = await keyvalues.get_value(dbsession, key)
            if empty_since_str is None:
                await keyvalues.set_value(dbsession, key, now.isoformat())
                continue
            try:
                empty_since = datetime.fromisoformat(empty_since_str)
            except ValueError:
                await keyvalues.set_value(dbsession, key, now.isoformat())
                continue
        if now - empty_since < SETTLE_DURATION:
            continue
        log.info("Removing unused network %s (empty since %s)", name, empty_since)
        try:
            await docker.remove_network(name)
            async with AsyncSession.begin() as dbsession:
                await keyvalues.delete_value(dbsession, key)
        except Exception:
            log.warning("Failed to remove network %s", name)
    await _remove_orphan_empty_since_entries(now)
    log.info("Done cleaning up Docker project networks")


async def _remove_orphan_empty_since_entries(now: datetime) -> None:
    async with AsyncSession.begin() as dbsession:
        entries = await keyvalues.all_key_values_with_prefix(dbsession, EMPTY_SINCE_KEY_PREFIX)
        for key, value in entries:
            if value is None:
                await keyvalues.delete_value(dbsession, key)
                continue
            try:
                empty_since = datetime.fromisoformat(value)
            except ValueError:
                await keyvalues.delete_value(dbsession, key)
                continue
            if now - empty_since >= ORPHAN_DURATION:
                log.info("Removing orphan key %s (empty since %s)", key, empty_since)
                await keyvalues.delete_value(dbsession, key)


def _network_is_empty(info: dict) -> bool:
    # Containers can be an object {"container_id": {...}, "...": {...}}
    # or null
    # Services can be an object {"service_name": {...}, "...": {...}}
    # or just not be present in the object
    return not info.get("Containers") and not info.get("Services")
