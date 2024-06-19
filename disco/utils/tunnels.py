import asyncio
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from disco.utils import docker

log = logging.getLogger(__name__)


@dataclass
class ActiveTunnel:
    expires: datetime
    service_name: str


tunnel_list_lock = asyncio.Lock()
_active_tunnels: list[ActiveTunnel] = []

TUNNEL_CMD = [
    "docker",
    "service",
    "create",
    "--name",
    "{name}",
    "--env",
    "PASSWORD={password}",
    "--publish",
    "published={host_port},target=22,protocol=tcp",
    "--network",
    "disco-main",
    "--label",
    "disco.tunnels",
    "letsdiscodev/sshtunnel",
]


def get_service_name(port: int) -> str:
    return f"disco-tunnel-{port}"


async def monitor_tunnel(service_name: str) -> None:
    global _active_tunnels
    log.info("Adding %s to the list of monitored tunnels", service_name)
    async with tunnel_list_lock:
        _active_tunnels.append(
            ActiveTunnel(
                service_name=service_name,
                expires=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )


async def extend_tunnel_expiration(service_name: str) -> None:
    global _active_tunnels
    log.info("Setting expiration of tunnel %d to 5 minutes from now", service_name)
    async with tunnel_list_lock:
        for tunnel in _active_tunnels:
            if tunnel.service_name == service_name:
                tunnel.expires = datetime.now(timezone.utc) + timedelta(minutes=5)
                return
    log.warning("Couldn't find active tunnel %s, not extending", service_name)


async def close_tunnel(service_name: str) -> None:
    global _active_tunnels
    log.info("Closing tunnel %s", service_name)
    active_tunnels = set(await get_active_tunnels())
    running_tunnels = await get_running_tunnels()
    if service_name in active_tunnels:
        async with tunnel_list_lock:
            for tunnel in _active_tunnels:
                if tunnel.service_name == service_name:
                    tunnel.expires = datetime.now(timezone.utc) - timedelta(minutes=999)
    if service_name in running_tunnels:
        await docker.stop_service(service_name)


async def get_active_tunnels() -> list[str]:
    global _active_tunnels
    async with tunnel_list_lock:
        _active_tunnels = [
            sl for sl in _active_tunnels if sl.expires > datetime.now(timezone.utc)
        ]
        return [sl.service_name for sl in _active_tunnels]


async def get_running_tunnels() -> list[str]:
    args = [
        "docker",
        "service",
        "ls",
        "--filter",
        "label=disco.tunnels",
        "--format",
        "{{ .Name }}",
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    services = stdout.decode("utf-8").split("\n")[:-1]
    return services


async def clean_up_tunnels() -> None:
    active_tunnels = set(await get_active_tunnels())
    running_tunnels = await get_running_tunnels()
    for running_tunnel in running_tunnels:
        if running_tunnel not in active_tunnels:
            log.warning("Killing rogue tunnel %s", running_tunnel)
            await docker.stop_service(running_tunnel)
