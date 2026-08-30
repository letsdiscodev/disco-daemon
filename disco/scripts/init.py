"""Script that runs when installing Disco on a server"""

import asyncio
import json
import logging
import os
import socket
import sqlite3
import sys

from alembic import command
from alembic.config import Config

import disco
from disco import config
from disco.models.db import Session, build_engines, get_engine
from disco.models.meta import base_metadata
from disco.utils import docker, keyvalues
from disco.utils.apikeys import create_api_key
from disco.utils.caddy import write_caddy_init_config
from disco.utils.dqlite import DQLITE_OVERLAY_NETWORK, bootstrap_first_node
from disco.utils.encryption import generate_key
from disco.utils.randomname import generate_random_name
from disco.utils.subprocess import check_call, check_call_streaming

log = logging.getLogger(__name__)

INIT_OPTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ha": ("false", ("false", "true")),
}


def main() -> None:
    asyncio.run(_main())


async def run_and_print(args: list[str]) -> None:
    async for line in check_call_streaming(args):
        print(line, end="", flush=True)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    options = parse_init_options(os.environ.get("DISCO_INIT_OPTIONS"))
    disco_host = os.environ.get("DISCO_HOST")
    disco_advertise_addr = os.environ.get("DISCO_ADVERTISE_ADDR")
    host_home = os.environ.get("HOST_HOME")
    image = os.environ.get("DISCO_IMAGE")
    cloudflare_tunnel_token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN")
    assert disco_host is not None
    assert disco_advertise_addr is not None
    assert host_home is not None
    assert image is not None
    ha = options["ha"] == "true"
    await create_caddy_socket_dir(host_home)
    await create_projects_dir(host_home)
    await create_static_site_dir(host_home)
    print("Initializing Docker Swarm")
    await create_docker_config(host_home)
    await docker_swarm_init(disco_advertise_addr)
    node_id = await get_this_swarm_node_id()
    await label_swarm_node(node_id, "disco-role=main")
    await docker.create_network("disco-main")
    await docker.create_network("disco-logging")
    await docker_swarm_create_disco_encryption_key()
    if ha:
        node_name = await generate_random_name()
        await label_swarm_node(node_id, f"disco-name={node_name}")
        await docker.create_network(DQLITE_OVERLAY_NETWORK)
        await docker.add_network_to_container(
            socket.gethostname(), DQLITE_OVERLAY_NETWORK
        )
        print("Starting dqlite")
        await bootstrap_first_node(node_name)
    await create_database(ha)
    print("Setting initial state in internal database")
    async with Session.begin() as dbsession:
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_VERSION", value=disco.__version__
        )
        await keyvalues.set_value(
            dbsession=dbsession,
            key="DISCO_ADVERTISE_ADDR",
            value=disco_advertise_addr,
        )
        await keyvalues.set_value(
            dbsession=dbsession, key="DISCO_HOST", value=disco_host
        )
        await keyvalues.set_value(dbsession=dbsession, key="HOST_HOME", value=host_home)
        await keyvalues.set_value(dbsession=dbsession, key="REGISTRY", value=None)
        if cloudflare_tunnel_token is not None:
            await keyvalues.set_value(
                dbsession=dbsession,
                key="CLOUDFLARE_TUNNEL_TOKEN",
                value=cloudflare_tunnel_token,
            )
        api_key = await create_api_key(dbsession=dbsession, name="First API key")
        print("Created API key:", api_key.id)
    print("Setting up Caddy web server")
    await write_caddy_init_config(
        disco_host, tunnel=cloudflare_tunnel_token is not None
    )
    await start_caddy(host_home, tunnel=cloudflare_tunnel_token is not None)
    print("Setting up Disco")
    await start_disco_daemon(host_home, image)
    if cloudflare_tunnel_token is not None:
        print("Setting up Cloudflare tunnel")
        await setup_cloudflare_tunnel(cloudflare_tunnel_token)


def parse_init_options(raw: str | None) -> dict[str, str]:
    options = {name: default for name, (default, _) in INIT_OPTIONS.items()}
    if raw is None:
        return options
    given = json.loads(raw)
    for name, value in given.items():
        if name not in INIT_OPTIONS:
            print(f"Unknown option: {name}", file=sys.stderr)
            sys.exit(2)
        _, allowed = INIT_OPTIONS[name]
        if value not in allowed:
            print(
                f"Invalid value for option {name}: {value} "
                f"(expected one of: {', '.join(allowed)})",
                file=sys.stderr,
            )
            sys.exit(2)
        options[name] = value
    return options


async def create_database(ha: bool) -> None:
    print("Creating Disco internal database")
    if not ha:

        def create_file() -> None:
            sqlite3.connect(config.SQLITE_PATH).close()

        await asyncio.get_event_loop().run_in_executor(None, create_file)
    await build_engines()
    async with get_engine().begin() as conn:
        await conn.run_sync(base_metadata.create_all)
        await conn.run_sync(_alembic_stamp_head)


def _alembic_stamp_head(connection) -> None:
    alembic_config = Config("/disco/app/alembic.ini")
    alembic_config.attributes["connection"] = connection
    command.stamp(alembic_config, "head")


async def docker_swarm_init(advertise_addr: str) -> None:
    await run_and_print(
        [
            "docker",
            "swarm",
            "init",
            "--advertise-addr",
            advertise_addr,
        ]
    )


async def docker_swarm_create_disco_encryption_key() -> None:
    print("Generating encryption key for encryption at rest")
    await check_call(
        [
            "docker",
            "secret",
            "create",
            "disco_encryption_key",
            "-",
        ],
        stdin=generate_key().decode("utf-8"),
    )


async def get_this_swarm_node_id() -> str:
    stdout, _, _ = await check_call(
        [
            "docker",
            "node",
            "inspect",
            "--format",
            "{{ .ID }}",
            "self",
        ]
    )
    return "".join(stdout).strip()


async def label_swarm_node(node_id: str, label: str) -> None:
    await run_and_print(
        [
            "docker",
            "node",
            "update",
            "--label-add",
            label,
            node_id,
        ]
    )


async def create_caddy_socket_dir(host_home: str) -> None:
    def makedirs() -> None:
        os.makedirs(f"/host{host_home}/disco/caddy-socket")

    await asyncio.get_event_loop().run_in_executor(None, makedirs)


async def start_caddy(host_home: str, tunnel: bool) -> None:
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
    await run_and_print(
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


async def create_projects_dir(host_home: str) -> None:
    def makedirs() -> None:
        os.makedirs(f"/host{host_home}/disco/projects")

    await asyncio.get_event_loop().run_in_executor(None, makedirs)


async def create_static_site_dir(host_home: str) -> None:
    def makedirs() -> None:
        os.makedirs(f"/host{host_home}/disco/srv")

    await asyncio.get_event_loop().run_in_executor(None, makedirs)


async def create_docker_config(host_home: str) -> None:
    # If the file doesn't exist, we create it so that we can mount it.
    # It's needed when we authenticate to a Docker Registry.
    def makedirs() -> None:
        path = f"/host{host_home}/.docker"
        if not os.path.isdir(path):
            os.makedirs(path)

    await asyncio.get_event_loop().run_in_executor(None, makedirs)


async def setup_cloudflare_tunnel(cloudflare_tunnel_token: str) -> None:
    await docker.create_network("disco-cloudflare-tunnel")
    await docker.add_network_to_container(
        "disco-caddy", "disco-cloudflare-tunnel", alias="disco-server"
    )
    await run_and_print(
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


async def start_disco_daemon(host_home: str, image: str) -> None:
    await run_and_print(
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
            *(["--network", DQLITE_OVERLAY_NETWORK] if config.is_ha() else []),
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
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            "--mount",
            "source=disco-caddy-data,target=/disco/caddy/data",
            "--mount",
            "source=disco-caddy-config,target=/disco/caddy/config",
            "--env",
            f"DISCO_IMAGE={image}",
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
