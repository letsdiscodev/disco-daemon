import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from disco import config
from disco.utils import docker
from disco.utils.subprocess import check_call

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
    "--log-driver",
    "json-file",
    "--log-opt",
    "max-size=20m",
    "--log-opt",
    "max-file=5",
    f"letsdiscodev/sshtunnel:{config.DISCO_TUNNEL_VERSION}",
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
    log.info("Setting expiration of tunnel %s to 5 minutes from now", service_name)
    async with tunnel_list_lock:
        for tunnel in _active_tunnels:
            if tunnel.service_name == service_name:
                tunnel.expires = datetime.now(timezone.utc) + timedelta(minutes=5)
                return
    log.warning("Couldn't find active tunnel %s, not extending", service_name)


async def close_tunnel(service_name: str) -> None:
    global _active_tunnels
    log.info("Closing tunnel %s", service_name)
    async with tunnel_list_lock:
        for tunnel in _active_tunnels:
            if tunnel.service_name == service_name:
                _active_tunnels.remove(tunnel)
        running_tunnels = await get_running_tunnels()
        if service_name in running_tunnels:
            await docker.rm_service(service_name)


async def get_active_tunnels() -> list[str]:
    global _active_tunnels
    async with tunnel_list_lock:
        return [sl.service_name for sl in _active_tunnels]


async def get_expired_tunnels() -> list[str]:
    global _active_tunnels
    async with tunnel_list_lock:
        return [
            sl.service_name
            for sl in _active_tunnels
            if sl.expires < datetime.now(timezone.utc)
        ]


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
    stdout, _, _ = await check_call(args)
    return stdout


async def stop_expired_tunnels() -> None:
    """Close tunnels that just expired within the last minute.

    If for some reason, the CLI doesn't tell Disco to close the tunnel,
    we catch that it expired and we close it.

    """
    expired_tunnels = await get_expired_tunnels()
    for expired_tunnel in expired_tunnels:
        log.warning("Killing expired tunnel %s", expired_tunnel)
        await close_tunnel(expired_tunnel)


async def clean_up_rogue_tunnels() -> None:
    """Close tunnels that could still run but we don't know about.

    E.g. if the server was restarted while a tunnel was running.
    The tunnel would still run but Disco wouldn't know about it.

    """
    active_tunnels = set(await get_active_tunnels())
    running_tunnels = await get_running_tunnels()
    for running_tunnel in running_tunnels:
        if running_tunnel not in active_tunnels:
            log.warning("Killing rogue tunnel %s", running_tunnel)
            await close_tunnel(running_tunnel)
