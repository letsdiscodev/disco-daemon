"""Script that runs when updating Disco to the latest version"""

import logging
import os
import re
import subprocess
from datetime import datetime, timedelta
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
    with Session() as dbsession:
        with dbsession.begin():
            installed_version = keyvalues.get_value(
                dbsession=dbsession, key="DISCO_VERSION"
            )
            assert installed_version is not None
            if installed_version == disco.__version__:
                print(f"Current version is latest ({disco.__version__}), not updating.")
                save_done_updating(dbsession)
                return
    print(f"Installed version: {installed_version}")
    print(f"New version: {installed_version}")
    print("Stopping existing Disco processes")
    stop_disco_daemon()
    if re.match(r"^0\.(1|2|3)\..+$", installed_version):
        stop_disco_worker()
    print("Running upgrade tasks")
    ttl = 9999
    while installed_version != disco.__version__:
        assert installed_version is not None
        task = get_update_function_for_version(installed_version)
        task()
        with Session() as dbsession:
            with dbsession.begin():
                installed_version = keyvalues.get_value(
                    dbsession=dbsession, key="DISCO_VERSION"
                )
        ttl -= 1
        if ttl < 0:
            print(
                f"Caught in an infinite loop while upgrading from {installed_version}"
            )
            break

    print("Starting new version of Disco")
    with Session() as dbsession:
        with dbsession.begin():
            host_home = keyvalues.get_value(dbsession=dbsession, key="HOST_HOME")
    assert host_home is not None
    start_disco_daemon(host_home, image)
    with Session() as dbsession:
        with dbsession.begin():
            save_done_updating(dbsession)


def _run_cmd(args: list[str], timeout=600) -> str:
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    timeout_dt = datetime.utcnow() + timedelta(seconds=timeout)
    output = ""
    for line in process.stdout:
        decoded_line = line.decode("utf-8")
        output += decoded_line
        print(decoded_line, end="", flush=True)
        if datetime.utcnow() > timeout_dt:
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


def task_0_3_x() -> None:
    print("Upating from 0.3.x to 0.4.x")
    alembic_upgrade("3eb8871ccb85")
    with Session() as dbsession:
        with dbsession.begin():
            keyvalues.set_value(dbsession=dbsession, key="DISCO_VERSION", value="0.4.0")


def task_0_2_x() -> None:
    print("Upating from 0.2.x to 0.3.x")
    alembic_upgrade("d0cba3cd3238")
    with Session() as dbsession:
        with dbsession.begin():
            keyvalues.set_value(dbsession=dbsession, key="DISCO_VERSION", value="0.3.0")


def task_0_1_x() -> None:
    print("Upating from 0.1.x to 0.2.x")
    alembic_upgrade("eba27af20db2")
    with Session() as dbsession:
        with dbsession.begin():
            keyvalues.set_value(dbsession=dbsession, key="DISCO_VERSION", value="0.2.0")


def task_patch() -> None:
    with Session() as dbsession:
        with dbsession.begin():
            keyvalues.set_value(
                dbsession=dbsession, key="DISCO_VERSION", value=disco.__version__
            )


def get_update_function_for_version(version: str) -> Callable[[], None]:
    if version.startswith("0.1."):
        return task_0_1_x
    if version.startswith("0.2."):
        return task_0_2_x
    if version.startswith("0.3."):
        return task_0_3_x
    if version.startswith("0.4."):
        assert disco.__version__.startswith("0.4.")
        return task_patch
    raise NotImplementedError(f"Update missing for version {version}")
