import asyncio
import logging
import subprocess

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey
from disco.utils import caddy, docker, keyvalues
from disco.utils.subprocess import decode_text

log = logging.getLogger(__name__)


def update_disco(
    dbsession: DBSession, image: str = "letsdiscodev/daemon:latest", pull: bool = True
) -> None:
    if is_updating(dbsession):
        raise Exception("An update is already in progress")
    save_is_updating(dbsession)
    if pull:
        asyncio.run(docker.pull(image))
    # The update script (disco_update) takes pre/post-upgrade backups, so
    # the container needs to (a) reach dqlite over the disco-dqlite overlay
    # and (b) write to the host-side backups directory the daemon also
    # mounts. Without these, the backups silently fail (the script catches
    # exceptions) and the update goes through with no restorable rollback
    # point.
    host_home = keyvalues.get_value_sync(dbsession, "HOST_HOME")
    assert host_home is not None
    _run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--detach",
            "--label",
            "disco.log.core=true",
            "--network",
            "disco-dqlite",
            "--env",
            f"DISCO_IMAGE={image}",
            "--mount",
            "source=disco-data,target=/disco/data",
            "--mount",
            f"type=bind,source={host_home}/disco/backups,target=/disco/backups",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            image,
            "disco_update",
        ]
    )


def _run_cmd(args: list[str], timeout=600) -> str:
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    output = ""
    for line in process.stdout:
        decoded_line = decode_text(line)
        output += decoded_line
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}:\n{output}")
    return output


def is_updating(dbsession: DBSession) -> bool:
    updating = keyvalues.get_value_sync(dbsession, "DISCO_IS_UPDATING")
    return updating is not None


def save_is_updating(dbsession: DBSession) -> None:
    keyvalues.set_value_sync(dbsession, "DISCO_IS_UPDATING", "true")


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
