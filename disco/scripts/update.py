"""Script that runs when updating Disco to the latest version"""
import logging
import os
import subprocess
from datetime import datetime, timedelta

import disco
from disco.models.db import Session
from disco.utils import keyvalues
from disco.utils.meta import save_done_updating

log = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO)
    with Session() as dbsession:
        with dbsession.begin():
            installed_version = keyvalues.get_value(
                dbsession=dbsession, key="DISCO_VERSION"
            )
            if installed_version == disco.__version__:
                print(f"Current version is latest ({disco.__version__}), not updating.")
                save_done_updating(dbsession)
                return
    print(f"Installed version: {installed_version}")
    print(f"New version: {installed_version}")
    print("Stopping existing Disco processes")
    stop_disco_daemon()
    stop_disco_worker()
    print("Running upgrade tasks")
    ttl = 9999
    while installed_version != disco.__version__:
        TASKS[installed_version]()
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
    start_disco_daemon(host_home)
    start_disco_worker(host_home)
    with Session() as dbsession:
        with dbsession.begin():
            save_done_updating(dbsession)


def _run_cmd(args: list[str], timeout=600) -> str:
    verbose = os.environ.get("DISCO_VERBOSE") == "true"
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
        if verbose:
            print(decoded_line, end="", flush=True)
        else:
            print(".", end="", flush=True)
        if datetime.utcnow() > timeout_dt:
            process.terminate()
            raise Exception(f"Running command failed, timeout after {timeout} seconds")
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}:\n{output}")
    if not verbose:
        print("", flush=True)
    return output


def start_disco_daemon(host_home: str) -> None:
    _run_cmd(
        [
            "docker",
            "service",
            "create",
            "--name",
            "disco",
            "--network",
            "disco-caddy-daemon",
            "--network",
            "disco-logging",
            "--mount",
            "source=disco-data,target=/disco/data",
            "--mount",
            f"type=bind,source={host_home}/.ssh,target=/root/.ssh",
            "--mount",
            f"type=bind,source={host_home}/.docker,target=/root/.docker",
            "--mount",
            f"type=bind,source={host_home}/disco/projects,target=/disco/projects",
            "--mount",
            f"type=bind,source={host_home}/disco/srv,target=/disco/srv",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "--mount",
            "type=bind,source=/var/run/caddy,target=/var/run/caddy",
            "--mount",
            "source=disco-certs,target=/certs",
            "--constraint",
            "node.labels.disco-role==main",
            f"letsdiscodev/daemon:{disco.__version__}",
            "uvicorn",
            "disco.app:app",
            "--port",
            "80",
            "--host",
            "0.0.0.0",
            "--root-path",
            "/.disco",
        ]
    )


def start_disco_worker(host_home: str) -> None:
    _run_cmd(
        [
            "docker",
            "service",
            "create",
            "--name",
            "disco-worker",
            "--network",
            "disco-caddy-daemon",
            "--network",
            "disco-logging",
            "--mount",
            "source=disco-data,target=/disco/data",
            "--mount",
            f"type=bind,source={host_home}/.ssh,target=/root/.ssh",
            "--mount",
            f"type=bind,source={host_home}/.docker,target=/root/.docker",
            "--mount",
            f"type=bind,source={host_home}/disco/projects,target=/disco/projects",
            "--mount",
            f"type=bind,source={host_home}/disco/srv,target=/disco/srv",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "--mount",
            "type=bind,source=/var/run/caddy,target=/var/run/caddy",
            "--mount",
            "source=disco-certs,target=/certs",
            "--constraint",
            "node.labels.disco-role==main",
            f"letsdiscodev/daemon:{disco.__version__}",
            "disco_worker",
        ]
    )


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


def task_0_1_0():
    print("Upating from 0.1.0 to 0.2.0")
    # TODO run Alembic migration, etc.
    with Session() as dbsession:
        with dbsession.begin():
            keyvalues.set_value(dbsession=dbsession, key="DISCO_VERSION", value="0.2.0")


TASKS = {"0.1.0": task_0_1_0}
