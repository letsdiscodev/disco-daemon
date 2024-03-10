"""Script that runs when updating Disco to the latest version"""

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Callable

from alembic import command
from alembic.config import Config

import disco
from disco.models.db import Session
from disco.scripts.init import start_disco_daemon
from disco.utils import keyvalues
from disco.utils.meta import save_done_updating

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    image = os.environ.get("DISCO_IMAGE")
    if image is None:  # backward compat for version <= 0.4.1
        image = "letsdiscodev/daemon:latest"
    with Session.begin() as dbsession:
        installed_version = keyvalues.get_value_sync(
            dbsession=dbsession, key="DISCO_VERSION"
        )
        assert installed_version is not None
    if installed_version == disco.__version__:
        print(f"Current version is latest ({disco.__version__}), not updating.")
        with Session.begin() as dbsession:
            save_done_updating(dbsession)
        return
    version_parts = installed_version.split(".")
    major = int(version_parts[0])
    minor = int(version_parts[1])
    if major == 0 and minor <= 4:
        with Session.begin() as dbsession:
            disco_host = keyvalues.get_value_sync(dbsession, "DISCO_HOST")
            disco_ip = keyvalues.get_value_sync(dbsession, "DISCO_IP")
            if disco_host == disco_ip:
                print("Must set Disco host first, not updating.")
                save_done_updating(dbsession)
                return
    print(f"Installed version: {installed_version}")
    print(f"New version: {installed_version}")
    print("Stopping existing Disco processes")
    try:
        stop_disco_daemon()
    except Exception:
        log.info("Failed to stop Disco")
    if re.match(r"^0\.(1|2|3)\..+$", installed_version):
        try:
            stop_disco_worker()
        except Exception:
            log.info("Failed to stop Disco Worker")
    print("Running upgrade tasks")
    ttl = 9999
    while installed_version != disco.__version__:
        assert installed_version is not None
        task = get_update_function_for_version(installed_version)
        task(image)
        with Session.begin() as dbsession:
            installed_version = keyvalues.get_value_sync(
                dbsession=dbsession, key="DISCO_VERSION"
            )
        ttl -= 1
        if ttl < 0:
            print(
                f"Caught in an infinite loop while upgrading from {installed_version}"
            )
            break

    print("Starting new version of Disco")
    with Session.begin() as dbsession:
        host_home = keyvalues.get_value_sync(dbsession=dbsession, key="HOST_HOME")
    assert host_home is not None
    start_disco_daemon(host_home, image)
    with Session.begin() as dbsession:
        save_done_updating(dbsession)


def _run_cmd(args: list[str], timeout=600) -> str:
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    timeout_dt = datetime.now(timezone.utc) + timedelta(seconds=timeout)
    output = ""
    for line in process.stdout:
        decoded_line = line.decode("utf-8")
        output += decoded_line
        print(decoded_line, end="", flush=True)
        if datetime.now(timezone.utc) > timeout_dt:
            process.terminate()
            raise Exception(f"Running command failed, timeout after {timeout} seconds")
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}:\n{output}")
    print("", flush=True)
    return output


def stop_disco_daemon() -> None:
    _run_cmd(
        [
            "docker",
            "service",
            "rm",
            "disco",
        ]
    )


def stop_disco_worker() -> None:
    _run_cmd(
        [
            "docker",
            "service",
            "rm",
            "disco-worker",
        ]
    )


def alembic_upgrade(version_hash: str) -> None:
    config = Config("/disco/app/alembic.ini")
    command.upgrade(config, version_hash)


def task_0_4_x(image: str) -> None:
    print("Upating from 0.4.x to 0.5.x")
    alembic_upgrade("87c62632dfd1")
    with Session.begin() as dbsession:
        disco_ip = keyvalues.get_value_sync(dbsession=dbsession, key="DISCO_IP")
        get_caddy_config_cmd = (
            "from disco.utils import caddy; "
            "import json; "
            "print(json.dumps(caddy.get_config()))"
        )
        caddy_config_str = _run_cmd(
            [
                "docker",
                "run",
                "--rm",
                "--mount",
                "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
                "--mount",
                "type=bind,source=/var/run/caddy,target=/var/run/caddy",
                image,
                "python",
                "-c",
                get_caddy_config_cmd,
            ]
        )
        caddy_config = json.loads(caddy_config_str)
        assert caddy_config is not None
        del caddy_config["apps"]["http"]["servers"]["disco"]["tls_connection_policies"]
        del caddy_config["apps"]["tls"]
        caddy_config["apps"]["http"]["servers"]["disco"]["routes"] = [
            route
            for route in caddy_config["apps"]["http"]["servers"]["disco"]["routes"]
            if route.get("@id") != "ip-handle"
        ]
        caddy_config_str = json.dumps(caddy_config)
        set_caddy_config_cmd = (
            "from disco.utils import caddy; "
            "import json; "
            f"caddy_config_str = '''{caddy_config_str}''';"
            "caddy_config = json.loads(caddy_config_str);"
            "caddy.set_config(caddy_config)"
        )
        _run_cmd(
            [
                "docker",
                "run",
                "--rm",
                "--mount",
                "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
                "--mount",
                "type=bind,source=/var/run/caddy,target=/var/run/caddy",
                image,
                "python",
                "-c",
                set_caddy_config_cmd,
            ]
        )
        keyvalues.set_value(
            dbsession=dbsession, key="DISCO_ADVERTISE_ADDR", value=disco_ip
        )
        keyvalues.delete_value(dbsession=dbsession, key="DISCO_IP")
        keyvalues.delete_value(dbsession=dbsession, key="PUBLIC_CA_CERT")
        keyvalues.set_value(dbsession=dbsession, key="DISCO_VERSION", value="0.5.0")


def task_0_3_x(image: str) -> None:
    print("Upating from 0.3.x to 0.4.x")
    alembic_upgrade("3eb8871ccb85")
    with Session.begin() as dbsession:
        keyvalues.set_value(dbsession=dbsession, key="DISCO_VERSION", value="0.4.0")


def task_0_2_x(image: str) -> None:
    print("Upating from 0.2.x to 0.3.x")
    alembic_upgrade("d0cba3cd3238")
    with Session.begin() as dbsession:
        keyvalues.set_value(dbsession=dbsession, key="DISCO_VERSION", value="0.3.0")


def task_0_1_x(image: str) -> None:
    print("Upating from 0.1.x to 0.2.x")
    alembic_upgrade("eba27af20db2")
    with Session.begin() as dbsession:
        keyvalues.set_value(dbsession=dbsession, key="DISCO_VERSION", value="0.2.0")


def task_patch(image: str) -> None:
    with Session.begin() as dbsession:
        keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value=disco.__version__
        )


def get_update_function_for_version(version: str) -> Callable[[str], None]:
    if version.startswith("0.1."):
        return task_0_1_x
    if version.startswith("0.2."):
        return task_0_2_x
    if version.startswith("0.3."):
        return task_0_3_x
    if version.startswith("0.4."):
        return task_0_4_x
    if version.startswith("0.5."):
        assert disco.__version__.startswith("0.5.")
        return task_patch
    raise NotImplementedError(f"Update missing for version {version}")
