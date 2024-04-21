import logging
import subprocess

from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey
from disco.utils import caddy, docker, keyvalues

log = logging.getLogger(__name__)


def update_disco(
    dbsession: DBSession, image: str = "letsdiscodev/daemon:latest", pull: bool = True
) -> None:
    if is_updating(dbsession):
        raise Exception("An update is already in progress")
    save_is_updating(dbsession)
    if pull:
        docker.pull(image)
    _run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--detach",
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


def _run_cmd(args: list[str], timeout=600) -> str:
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    output = ""
    for line in process.stdout:
        decoded_line = line.decode("utf-8")
        output += decoded_line
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}:\n{output}")
    return output


def is_updating(dbsession: DBSession) -> bool:
    updating = keyvalues.get_value(dbsession, "DISCO_IS_UPDATING")
    return updating is not None


def save_is_updating(dbsession: DBSession) -> None:
    keyvalues.set_value(dbsession, "DISCO_IS_UPDATING", "true")


def save_done_updating(dbsession: DBSession) -> None:
    keyvalues.delete_value(dbsession, "DISCO_IS_UPDATING")


def set_disco_host(dbsession: DBSession, host: str, by_api_key: ApiKey) -> None:
    prev_host = keyvalues.get_value(dbsession=dbsession, key="DISCO_HOST")
    log.info(
        "Setting Disco host from %s to %s by %s", prev_host, host, by_api_key.log()
    )
    caddy.update_disco_host(host)
    keyvalues.set_value(dbsession=dbsession, key="DISCO_HOST", value=host)
