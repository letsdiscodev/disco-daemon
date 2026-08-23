"""Script that runs when installing Disco on a server"""

import asyncio
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

from alembic import command
from alembic.config import Config

import disco
from disco import config
from disco.models.meta import base_metadata
from disco.utils import docker, keyvalues
from disco.utils.apikeys import create_api_key_sync
from disco.utils.dqlite import (
    dqlite_service_name,
    start_first_dqlite_service_sync,
    strip_bootstrap_allowed_sync,
    wait_for_dqlite_service_healthy_sync,
)
from disco.utils.encryption import generate_key
from disco.utils.randomname import generate_random_name_sync
from disco.utils.subprocess import decode_text

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    disco_host = os.environ.get("DISCO_HOST")
    disco_advertise_addr = os.environ.get("DISCO_ADVERTISE_ADDR")
    host_home = os.environ.get("HOST_HOME")
    image = os.environ.get("DISCO_IMAGE")
    cloudflare_tunnel_token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN")
    disco_mode = os.environ.get("DISCO_MODE", "sqlite")
    assert disco_host is not None
    assert disco_advertise_addr is not None
    assert host_home is not None
    assert image is not None
    assert disco_mode in ("sqlite", "dqlite")

    create_caddy_socket_dir(host_home)
    create_projects_dir(host_home)
    create_static_site_dir(host_home)
    create_backups_dir(host_home)
    print("Initializing Docker Swarm")
    create_docker_config(host_home)
    docker_swarm_init(disco_advertise_addr)

    node_id = get_this_swarm_node_id()
    label_swarm_node(node_id, "disco-role=main")

    asyncio.run(docker.create_network("disco-main"))
    asyncio.run(docker.create_network("disco-logging"))

    docker_swarm_create_disco_encryption_key()

    # Persist the mode to the disco-data volume before any DB access so
    # every later component (this process included) resolves the same mode.
    # The sqlite flow has the volume mounted (it writes the sqlite file
    # in-process); the dqlite flow may not, so it goes through a one-shot
    # container instead.
    if disco_mode == "sqlite":
        config.write_disco_mode("sqlite")
    else:
        write_disco_mode_to_volume(image, "dqlite")
        config.pin_mode("dqlite")

    if disco_mode == "dqlite":
        _init_dqlite_mode(
            node_id=node_id,
            image=image,
            disco_host=disco_host,
            disco_advertise_addr=disco_advertise_addr,
            host_home=host_home,
            cloudflare_tunnel_token=cloudflare_tunnel_token,
        )
    else:
        _init_sqlite_mode(
            disco_host=disco_host,
            disco_advertise_addr=disco_advertise_addr,
            host_home=host_home,
            image=image,
            cloudflare_tunnel_token=cloudflare_tunnel_token,
        )
    if cloudflare_tunnel_token is not None:
        print("Setting up Cloudflare tunnel")
        setup_cloudflare_tunnel(cloudflare_tunnel_token)


def _init_sqlite_mode(
    disco_host: str,
    disco_advertise_addr: str,
    host_home: str,
    image: str,
    cloudflare_tunnel_token: str | None,
) -> None:
    from disco.utils.caddy import write_caddy_init_config

    create_database()
    print("Setting initial state in internal database")
    _seed_initial_state(
        disco_host=disco_host,
        disco_advertise_addr=disco_advertise_addr,
        host_home=host_home,
        cloudflare_tunnel_token=cloudflare_tunnel_token,
    )
    print("Setting up Caddy web server")
    write_caddy_init_config(disco_host, tunnel=cloudflare_tunnel_token is not None)
    start_caddy_container(host_home, tunnel=cloudflare_tunnel_token is not None)
    print("Setting up Disco")
    start_disco_daemon(host_home, image)


def _init_dqlite_mode(
    node_id: str,
    image: str,
    disco_host: str,
    disco_advertise_addr: str,
    host_home: str,
    cloudflare_tunnel_token: str | None,
) -> None:
    disco_name = generate_random_name_sync()
    print(f"Generated disco-name for this node: {disco_name}")
    label_swarm_node(node_id, f"disco-name={disco_name}")

    asyncio.run(docker.create_network("disco-dqlite"))

    print("Starting dqlite cluster (bootstrap node)")
    start_first_dqlite_service_sync(disco_name)
    print("Waiting for dqlite to be ready")
    wait_for_dqlite_service_healthy_sync(dqlite_service_name(disco_name))
    # Strip BOOTSTRAP_ALLOWED so a future volume loss can't accidentally
    # bootstrap a second cluster. This rolls the task once; dqlite-demo
    # picks up the persisted bind address (a DNS name) and resumes.
    print("Disabling bootstrap mode")
    strip_bootstrap_allowed_sync(disco_name)
    wait_for_dqlite_service_healthy_sync(dqlite_service_name(disco_name))

    init_database(
        image=image,
        disco_host=disco_host,
        disco_advertise_addr=disco_advertise_addr,
        host_home=host_home,
        cloudflare_tunnel_token=cloudflare_tunnel_token,
    )
    print("Setting up Caddy web server")
    start_caddy_service(
        host_home,
        tunnel=cloudflare_tunnel_token is not None,
        caddy_image=config.get_caddy_image(),
    )
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
        decoded_line = decode_text(line)
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
    from disco.models.db import get_engine

    engine = get_engine()
    base_metadata.create_all(engine)
    alembic_config = Config("/disco/app/alembic.ini")
    command.stamp(alembic_config, "head")


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


def create_caddy_socket_dir(host_home: str) -> None:
    os.makedirs(f"/host{host_home}/disco/caddy-socket")


def write_disco_mode_to_volume(image: str, mode: str) -> None:
    # One-shot container write so this works no matter which mounts the
    # installer gave the init container.
    _run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            "source=disco-data,target=/disco/data",
            image,
            "sh",
            "-c",
            f"echo {mode} > {config.DISCO_MODE_FILE}",
        ]
    )


def start_caddy_container(host_home: str, tunnel: bool) -> None:
    """sqlite mode: run stock Caddy as a plain container on this node.

    TLS certs live on the disco-caddy-data volume and config persists via
    Caddy's autosave on the disco-caddy-config volume; --resume makes the
    autosave win over the initconfig once it exists.
    """
    more_args = []
    if not tunnel:
        more_args += [
            "--publish",
            "published=80,target=80,protocol=tcp",
            "--publish",
            "published=443,target=443,protocol=tcp",
            "--publish",
            "published=443,target=443,protocol=udp",
        ]
    _run_cmd(
        [
            "docker",
            "run",
            "--name",
            "disco-caddy",
            "--detach",
            "--restart",
            "always",
            "--mount",
            "source=disco-caddy-data,target=/data",
            "--mount",
            "source=disco-caddy-config,target=/config",
            "--network",
            "disco-main",
            "--mount",
            f"type=bind,source={host_home}/disco/caddy-socket,target=/disco/caddy-socket",
            "--mount",
            "source=disco-caddy-init-config,target=/initconfig",
            "--mount",
            f"type=bind,source={host_home}/disco/srv,target=/disco/srv",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=20m",
            "--log-opt",
            "max-file=5",
            *more_args,
            f"caddy:{config.CADDY_VERSION}",
            "caddy",
            "run",
            "--resume",
            "--config",
            "/initconfig/config.json",
        ]
    )


def start_caddy_service(host_home: str, tunnel: bool, caddy_image: str) -> None:
    """dqlite mode: run Caddy as a global Swarm service (one task per node) for HA ingress.

    TLS certs and routing config live in dqlite (shared by all instances), so no
    cert/config volumes. Each task starts from the baked-in minimal bootstrap and
    the config-sync module pulls the real config from dqlite; the daemon seeds the
    base config on startup. Ports 80/443 are published in host mode to preserve
    the real client IP. The bind-mount source dirs are created by Swarm on each
    node if absent.
    """
    from disco.utils.dqlite import get_local_dqlite_address

    publish_args = []
    if not tunnel:
        publish_args += [
            "--publish",
            "published=80,target=80,protocol=tcp,mode=host",
            "--publish",
            "published=443,target=443,protocol=tcp,mode=host",
            "--publish",
            "published=443,target=443,protocol=udp,mode=host",
        ]
    _run_cmd(
        [
            "docker",
            "service",
            "create",
            "--name",
            "disco-caddy",
            "--mode",
            "global",
            "--network",
            "disco-main",
            "--network",
            "disco-dqlite",
            "--env",
            # One reachable node is enough to bootstrap; go-dqlite discovers the
            # rest. The reconciler keeps this in sync as nodes join/leave.
            f"DISCO_DQLITE_NODES={get_local_dqlite_address()}",
            "--mount",
            f"type=bind,source={host_home}/disco/caddy-socket,target=/disco/caddy-socket",
            "--mount",
            f"type=bind,source={host_home}/disco/srv,target=/disco/srv",
            "--container-label",
            "disco.log.core=true",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=20m",
            "--log-opt",
            "max-file=5",
            *publish_args,
            caddy_image,
            "caddy",
            "run",
            "--config",
            "/etc/caddy/bootstrap.json",
        ]
    )


def create_projects_dir(host_home: str) -> None:
    os.makedirs(f"/host{host_home}/disco/projects")


def create_static_site_dir(host_home: str) -> None:
    os.makedirs(f"/host{host_home}/disco/srv")


def create_backups_dir(host_home: str) -> None:
    # Holds the dqlite backup file (latest.db + rotated timestamped copies).
    # Bind-mounted into the daemon container and into one-shot tooling
    # (disco_update, disco_restore) that needs to read/write it.
    os.makedirs(f"/host{host_home}/disco/backups", exist_ok=True)


def create_docker_config(host_home: str) -> None:
    # If the file doesn't exist, we create it so that we can mount it.
    # It's needed when we authenticate to a Docker Registry.
    path = f"/host{host_home}/.docker"
    if not os.path.isdir(path):
        os.makedirs(f"/host{host_home}/.docker")


def _resolve_caddy_container() -> str:
    """Name/id of the local Caddy container to attach networks to.

    sqlite mode: the plain container is named disco-caddy. dqlite mode: Caddy
    is a global Swarm service, so the local task container has a generated
    name — resolve it via docker ps. Note that an out-of-band network attach
    to a service task does not survive the task being rescheduled; multi-node
    tunnel semantics are still an open question for dqlite mode.
    """
    if not config.is_dqlite_mode():
        return "disco-caddy"
    for _ in range(30):
        output = _run_cmd(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                "name=disco-caddy",
                "--filter",
                "status=running",
            ]
        ).strip()
        if output:
            return output.splitlines()[0]
        time.sleep(1)
    raise Exception("No running disco-caddy task container found")


def setup_cloudflare_tunnel(cloudflare_tunnel_token: str) -> None:
    asyncio.run(docker.create_network("disco-cloudflare-tunnel"))
    docker.add_network_to_container_sync(
        _resolve_caddy_container(), "disco-cloudflare-tunnel", alias="disco-server"
    )
    _run_cmd(
        [
            "docker",
            "run",
            "--name",
            "cloudflared",
            "--detach",
            "--restart",
            "always",
            "--network",
            "disco-cloudflare-tunnel",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=20m",
            "--log-opt",
            "max-file=5",
            "cloudflare/cloudflared:latest",
            "tunnel",
            "--no-autoupdate",
            "run",
            "--token",
            cloudflare_tunnel_token,
        ]
    )


def start_disco_daemon(host_home: str, image: str) -> None:
    if config.is_dqlite_mode():
        mode_args = [
            "--network",
            "disco-dqlite",
        ]
    else:
        # Cert storage and Caddy's autosaved config live on volumes the
        # daemon needs to read (project export) and update tasks rewrite.
        mode_args = [
            "--mount",
            "source=disco-caddy-data,target=/disco/caddy/data",
            "--mount",
            "source=disco-caddy-config,target=/disco/caddy/config",
        ]
    _run_cmd(
        [
            "docker",
            "service",
            "create",
            "--name",
            "disco",
            "--network",
            "disco-main",
            "--network",
            "disco-logging",
            *mode_args,
            "--container-label",
            "disco.log.core=true",
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
            f"type=bind,source={host_home}/disco/caddy-socket,target=/disco/caddy-socket",
            "--mount",
            f"type=bind,source={host_home}/disco/backups,target=/disco/backups",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "--env",
            f"DISCO_IMAGE={image}",
            "--env",
            # Belt and braces on top of the disco-mode marker file.
            f"DISCO_MODE={config.get_disco_mode()}",
            "--secret",
            "disco_encryption_key",
            "--constraint",
            "node.labels.disco-role==main",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=20m",
            "--log-opt",
            "max-file=5",
            image,
            "uvicorn",
            "disco.app:app",
            "--port",
            "80",
            "--host",
            "0.0.0.0",
        ]
    )


def init_database(
    image: str,
    disco_host: str,
    disco_advertise_addr: str,
    host_home: str,
    cloudflare_tunnel_token: str | None,
) -> None:
    more_args = []
    if cloudflare_tunnel_token:
        more_args += [
            "--env",
            f"CLOUDFLARE_TUNNEL_TOKEN={cloudflare_tunnel_token}",
        ]
    output = _run_cmd(
        [
            "docker",
            "run",
            "--rm",
            "--env",
            "DISCO_MODE=dqlite",
            "--env",
            f"DISCO_HOST={disco_host}",
            "--env",
            f"DISCO_ADVERTISE_ADDR={disco_advertise_addr}",
            "--env",
            f"HOST_HOME={host_home}",
            *more_args,
            "--network",
            "disco-dqlite",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=20m",
            "--log-opt",
            "max-file=5",
            image,
            "disco_init_db",
        ]
    )
    print(output)


def _seed_initial_state(
    disco_host: str,
    disco_advertise_addr: str,
    host_home: str,
    cloudflare_tunnel_token: str | None,
) -> None:
    from disco.models.db import Session

    with Session.begin() as dbsession:
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_VERSION", value=disco.__version__
        )
        keyvalues.set_value_sync(
            dbsession=dbsession,
            key="DISCO_ADVERTISE_ADDR",
            value=disco_advertise_addr,
        )
        keyvalues.set_value_sync(
            dbsession=dbsession, key="DISCO_HOST", value=disco_host
        )
        keyvalues.set_value_sync(dbsession=dbsession, key="HOST_HOME", value=host_home)
        keyvalues.set_value_sync(dbsession=dbsession, key="REGISTRY", value=None)
        if cloudflare_tunnel_token is not None:
            keyvalues.set_value_sync(
                dbsession=dbsession,
                key="CLOUDFLARE_TUNNEL_TOKEN",
                value=cloudflare_tunnel_token,
            )
        api_key = create_api_key_sync(dbsession=dbsession, name="First API key")
        print("Created API key:", api_key.id)


def init_database_script():
    # dqlite-mode one-shot (disco_init_db): runs on the disco-dqlite network
    # without the disco-data mount, so pin the mode explicitly.
    config.pin_mode("dqlite")
    disco_host = os.environ.get("DISCO_HOST")
    disco_advertise_addr = os.environ.get("DISCO_ADVERTISE_ADDR")
    host_home = os.environ.get("HOST_HOME")
    cloudflare_tunnel_token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN")
    assert disco_host is not None
    assert disco_advertise_addr is not None
    assert host_home is not None

    # Now that dqlite is ready, create the database
    create_database()

    print("Setting initial state in internal database")
    _seed_initial_state(
        disco_host=disco_host,
        disco_advertise_addr=disco_advertise_addr,
        host_home=host_home,
        cloudflare_tunnel_token=cloudflare_tunnel_token,
    )
