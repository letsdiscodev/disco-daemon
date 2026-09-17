import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession as DBSession

from disco import config
from disco.models import ApiKey
from disco.utils import caddy, docker, keyvalues
from disco.utils.dqlite import DQLITE_OVERLAY_NETWORK
from disco.utils.subprocess import decode_text

log = logging.getLogger(__name__)


async def update_disco(
    dbsession: DBSession,
    image: str = "letsdiscodev/daemon:latest",
    pull: bool = True,
) -> None:
    if await is_updating(dbsession):
        raise Exception("An update is already in progress")
    await save_is_updating(dbsession)
    if pull:
        await docker.pull(image)
    await _run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--detach",
            "--label",
            "disco.log.core=true",
            *(["--network", DQLITE_OVERLAY_NETWORK] if config.is_ha() else []),
            "--env",
            f"DISCO_IMAGE={image}",
            "--mount",
            "source=disco-data,target=/disco/data",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            image,
            "disco_update",
        ]
    )


async def _run_cmd(args: list[str]) -> str:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert process.stdout is not None
    output = ""
    async for line in process.stdout:
        output += decode_text(line)
    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}:\n{output}")
    return output


async def is_updating(dbsession: DBSession) -> bool:
    updating = await keyvalues.get_value(dbsession, "DISCO_IS_UPDATING")
    return updating is not None


async def save_is_updating(dbsession: DBSession) -> None:
    await keyvalues.set_value(dbsession, "DISCO_IS_UPDATING", "true")


async def save_done_updating(dbsession: DBSession) -> None:
    await keyvalues.delete_value(dbsession, "DISCO_IS_UPDATING")


async def set_disco_host(dbsession: DBSession, host: str, by_api_key: ApiKey) -> None:
    prev_host = await keyvalues.get_value_str(dbsession=dbsession, key="DISCO_HOST")
    log.info(
        "Setting Disco host from %s to %s by %s", prev_host, host, by_api_key.log()
    )
    await caddy.update_disco_host(host)
    await keyvalues.set_value(dbsession=dbsession, key="DISCO_HOST", value=host)
    # the hostname is part of each collector's config: one reconcile replaces every
    # collector (new ones first, a settle, then the old ones go)
    from disco.utils.syslog import (
        get_destination_buffer_bytes,
        get_syslog_urls,
        set_syslog_services,
    )

    syslog_urls = await get_syslog_urls(dbsession)
    buffer_bytes = await get_destination_buffer_bytes(dbsession)
    await set_syslog_services(host, syslog_urls, buffer_bytes)
