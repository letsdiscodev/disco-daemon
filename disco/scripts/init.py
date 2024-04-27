"""Script that runs when installing Disco on a server"""

import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone

from alembic import command
from alembic.config import Config

import disco
from disco import config
from disco.models.db import Session, engine
from disco.models.meta import base_metadata
from disco.utils import docker, keyvalues
from disco.utils.apikeys import create_api_key
from disco.utils.caddy import write_caddy_init_config
from disco.utils.encryption import generate_key

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    disco_host = os.environ.get("DISCO_HOST")
    disco_advertise_addr = os.environ.get("DISCO_ADVERTISE_ADDR")
    host_home = os.environ.get("HOST_HOME")
    image = os.environ.get("DISCO_IMAGE")
    assert disco_host is not None
    assert disco_advertise_addr is not None
    assert host_home is not None
    assert image is not None
    create_database()
    print("Setting initial state in internal database")
    with Session() as dbsession:
        with dbsession.begin():
            keyvalues.set_value(
                dbsession=dbsession, key="DISCO_VERSION", value=disco.__version__
            )
            keyvalues.set_value(
                dbsession=dbsession,
                key="DISCO_ADVERTISE_ADDR",
                value=disco_advertise_addr,
            )
            keyvalues.set_value(dbsession=dbsession, key="DISCO_HOST", value=disco_host)
            keyvalues.set_value(dbsession=dbsession, key="HOST_HOME", value=host_home)
            keyvalues.set_value(dbsession=dbsession, key="REGISTRY_HOST", value=None)
            api_key = create_api_key(dbsession=dbsession, name="First API key")
            print("Created API key:", api_key.id)
    create_caddy_socket_dir()
    create_projects_dir(host_home)
    create_static_site_dir(host_home)
    print("Initializing Docker Swarm")
    create_docker_config(host_home)
    docker_swarm_init(disco_advertise_addr)
    node_id = get_this_swarm_node_id()
    label_swarm_node(node_id, "disco-role=main")
    docker.create_network("disco-caddy-daemon")
    docker.create_network("disco-logging")
    docker_swarm_create_disco_encryption_key()
    print("Setting up Caddy web server")
    write_caddy_init_config(disco_host)
    start_caddy(host_home)
    print("Setting up Disco")
    start_disco_daemon(host_home, image)


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


def create_database():
    print("Creating Disco internal database")
    base_metadata.create_all(engine)
    config = Config("/disco/app/alembic.ini")
    command.stamp(config, "head")


def docker_swarm_init(advertise_addr: str) -> None:
    _run_cmd(
        [
            "docker",
            "swarm",
            "init",
            "--advertise-addr",
            advertise_addr,
        ]
    )


def docker_swarm_create_disco_encryption_key() -> None:
    print("Generating encryption key for encryption at rest")
    process = subprocess.Popen(
        args=[
            "docker",
            "secret",
            "create",
            "disco_encryption_key",
            "-",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    _, _ = process.communicate(generate_key())
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def get_this_swarm_node_id() -> str:
    output = _run_cmd(
        [
            "docker",
            "node",
            "inspect",
            "--format",
            "{{ .ID }}",
            "self",
        ]
    )
    node_id = output.replace("\n", "")
    return node_id


def label_swarm_node(node_id: str, label: str) -> None:
    _run_cmd(
        [
            "docker",
            "node",
            "update",
            "--label-add",
            label,
            node_id,
        ]
    )


def create_caddy_socket_dir() -> None:
    os.makedirs("/host/var/run/caddy")


def start_caddy(host_home: str) -> None:
    _run_cmd(
        [
            "docker",
            "run",
            "--name",
            "disco-caddy",
            "--detach",
            "--restart",
            "always",
            "--publish",
            "published=80,target=80,protocol=tcp",
            "--publish",
            "published=443,target=443,protocol=tcp",
            "--publish",
            "published=443,target=443,protocol=udp",
            "--mount",
            "source=disco-caddy-data,target=/data",
            "--mount",
            "source=disco-caddy-config,target=/config",
            "--network",
            "disco-caddy-daemon",
            "--mount",
            "type=bind,source=/var/run/caddy,target=/var/run/caddy",
            "--mount",
            "source=disco-caddy-init-config,target=/initconfig",
            "--mount",
            f"type=bind,source={host_home}/disco/srv,target=/disco/srv",
            f"caddy:{config.CADDY_VERSION}",
            "caddy",
            "run",
            "--resume",
            "--config",
            "/initconfig/config.json",
        ]
    )


def create_projects_dir(host_home) -> None:
    os.makedirs(f"/host{host_home}/disco/projects")


def create_static_site_dir(host_home) -> None:
    os.makedirs(f"/host{host_home}/disco/srv")


def create_docker_config(host_home) -> None:
    # If the file doesn't exist, we create it so that we can mount it.
    # It's needed when we authenticate to a Docker Registry.
    path = f"/host{host_home}/.docker"
    if not os.path.isdir(path):
        os.makedirs(f"/host{host_home}/.docker")


def start_disco_daemon(host_home: str, image: str) -> None:
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
            "source=disco-caddy-data,target=/disco/caddy/data",
            "--mount",
            "source=disco-caddy-config,target=/disco/caddy/config",
            "--secret",
            "disco_encryption_key",
            "--constraint",
            "node.labels.disco-role==main",
            image,
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
