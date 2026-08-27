import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco import config
from disco.models import ApiKey
from disco.utils import caddy, docker, keyvalues
from disco.utils.dqlite import DQLITE_OVERLAY_NETWORK
from disco.utils.subprocess import decode_text

log = logging.getLogger(__name__)


async def update_disco(
    dbsession: AsyncDBSession,
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


async def is_updating(dbsession: AsyncDBSession) -> bool:
    updating = await keyvalues.get_value(dbsession, "DISCO_IS_UPDATING")
    return updating is not None


async def save_is_updating(dbsession: AsyncDBSession) -> None:
    await keyvalues.set_value(dbsession, "DISCO_IS_UPDATING", "true")


def save_done_updating(dbsession: DBSession) -> None:
    keyvalues.delete_value_sync(dbsession, "DISCO_IS_UPDATING")


async def set_disco_host(
    dbsession: AsyncDBSession, host: str, by_api_key: ApiKey
) -> None:
    from disco.utils import docker

    prev_host = await keyvalues.get_value_str(dbsession=dbsession, key="DISCO_HOST")
    log.info(
        "Setting Disco host from %s to %s by %s", prev_host, host, by_api_key.log()
    )
    await caddy.update_disco_host(host)
    await keyvalues.set_value(dbsession=dbsession, key="DISCO_HOST", value=host)
    syslog_services = await docker.list_syslog_services()
    for syslog_service in syslog_services:
        await docker.update_syslog_hostname(
            service_name=syslog_service.name, disco_host=host
        )
