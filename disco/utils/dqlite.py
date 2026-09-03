import asyncio
import logging
import time
from dataclasses import dataclass

from disco.utils.subprocess import call, check_call

log = logging.getLogger(__name__)

DQLITE_IMAGE_TAG = "0.2.0"
DQLITE_PORT = 9001
DQLITE_API_PORT = 10001
DQLITE_OVERLAY_NETWORK = "disco-dqlite"

_join_lock = asyncio.Lock()


def dqlite_service_name(node_name: str) -> str:
    return f"dqlite-{node_name}"


def dqlite_bind_address(node_name: str) -> str:
    return f"tasks.{dqlite_service_name(node_name)}:{DQLITE_PORT}"


@dataclass
class DqliteService:
    name: str
    running: bool


async def list_dqlite_services() -> list[DqliteService]:
    stdout, _, _ = await check_call(
        [
            "docker",
            "service",
            "ls",
            "--filter",
            "label=disco.service=dqlite",
            "--format",
            "{{ .Name }} {{ .Replicas }}",
        ]
    )
    services = []
    for line in stdout:
        parts = line.split()
        if len(parts) != 2:
            continue
        name, replicas = parts
        services.append(DqliteService(name=name, running=replicas == "1/1"))
    return services


async def create_missing_dqlite_services(node_names: list[str]) -> None:
    """When a node doesn't have dqlite running, start it and join the cluster.

    Starts maximum one dqlite service, since we need to wait for it to join
    before starting others.

    """
    if _join_lock.locked():
        return
    async with _join_lock:
        running = {}
        for service in await list_dqlite_services():
            running[service.name] = service.running
        with_service = []
        without_service = []
        for node_name in node_names:
            if dqlite_service_name(node_name) in running:
                with_service.append(node_name)
            else:
                log.info("Node %s has no dqlite service", node_name)
                without_service.append(node_name)
        if len(without_service) == 0:
            return
        if (
            service_name := await _dqlite_still_starting(with_service, running)
        ) is not None:
            log.info("Not adding dqlite members while %s is not ready", service_name)
            return
        node_name = without_service[0]
        peers: list[str] = []
        for name in with_service:
            if name == current_node_name():
                # init-node uses the first one in the list
                peers.insert(0, dqlite_bind_address(name))
            else:
                peers.append(dqlite_bind_address(name))
        if len(peers) == 0:
            log.error("No dqlite service to join for node %s", node_name)
            return
        await start_dqlite_service(
            node_name=node_name, peers=peers, bootstrap_allowed=False
        )


async def _dqlite_still_starting(
    with_service: list[str], running: dict[str, bool]
) -> str | None:
    """Returns the name of the service of the first dqlite that's not fully started."""
    for node_name in with_service:
        service_name = dqlite_service_name(node_name)
        if not running[service_name] or not await _dqlite_member_ready(node_name):
            return service_name
    return None


async def _dqlite_member_ready(node_name: str) -> bool:
    # the API port only opens once the node is a member of the cluster
    host = f"tasks.{dqlite_service_name(node_name)}"
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, DQLITE_API_PORT), timeout=2
        )
    except (OSError, TimeoutError):
        return False
    writer.close()
    return True


_node_name: str | None = None


async def resolve_node_name() -> None:
    global _node_name
    if _node_name is not None:
        return
    stdout, _, _ = await check_call(
        [
            "docker",
            "node",
            "inspect",
            "--format",
            '{{ index .Spec.Labels "disco-name" }}',
            "self",
        ]
    )
    name = "".join(stdout).strip()
    if not name:
        raise RuntimeError("self node has no disco-name label")
    _node_name = name


def current_node_name() -> str:
    if _node_name is None:
        # because this function can't be async
        raise RuntimeError("node name not resolved, call resolve_node_name() first")
    return _node_name


def get_local_dqlite_address() -> str:
    return dqlite_bind_address(current_node_name())


async def start_dqlite_service(
    *,
    node_name: str,
    peers: list[str] | None,
    bootstrap_allowed: bool,
) -> None:
    service_name = dqlite_service_name(node_name)
    bind = dqlite_bind_address(node_name)
    env_args: list[str] = ["--env", f"BIND_ADDRESS={bind}"]
    if bootstrap_allowed:
        env_args += ["--env", "BOOTSTRAP_ALLOWED=true"]
    if peers:
        env_args += ["--env", f"PEERS={','.join(peers)}"]
    log.info(
        "Creating dqlite service %s bind=%s peers=%s bootstrap=%s",
        service_name,
        bind,
        peers or [],
        bootstrap_allowed,
    )
    await check_call(
        [
            "docker",
            "service",
            "create",
            "--detach",
            "--name",
            service_name,
            "--replicas",
            "1",
            "--endpoint-mode",
            "dnsrr",
            "--network",
            DQLITE_OVERLAY_NETWORK,
            "--constraint",
            f"node.labels.disco-name=={node_name}",
            "--mount",
            f"source=dqlite-{node_name},target=/data",
            "--label",
            "disco.service=dqlite",
            "--label",
            f"disco.dqlite.disco-name={node_name}",
            *env_args,
            "--health-cmd",
            # /app/healthcheck.sh is too strict
            # leader has to reach raft port so that dqlite opens API port
            f"nc -z -w 2 tasks.{service_name} {DQLITE_PORT}",
            "--health-interval",
            "5s",
            "--health-start-period",
            "60s",
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=20m",
            f"letsdiscodev/dqlite:{DQLITE_IMAGE_TAG}",
        ]
    )


async def disable_bootstrap(node_name: str) -> None:
    service_name = dqlite_service_name(node_name)
    log.info("Stripping BOOTSTRAP_ALLOWED from %s", service_name)
    await check_call(
        [
            "docker",
            "service",
            "update",
            "--env-rm",
            "BOOTSTRAP_ALLOWED",
            "--force",
            service_name,
        ]
    )


async def wait_for_dqlite_service_healthy(
    service_name: str, timeout_seconds: int = 180
) -> None:
    log.info("Waiting for %s to become healthy", service_name)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        stdout, _, _ = await check_call(
            [
                "docker",
                "service",
                "ps",
                service_name,
                "--filter",
                "desired-state=running",
                "--format",
                "{{ .ID }}|{{ .CurrentState }}",
            ]
        )
        running_task_id: str | None = None
        for line in stdout:
            try:
                task_id, current = line.split("|", 1)
            except ValueError:
                continue
            if current.startswith("Running"):
                running_task_id = task_id
                break
        if running_task_id is None:
            await asyncio.sleep(2)
            continue
        if await _task_is_healthy(running_task_id):
            log.info("%s has a healthy task", service_name)
            return
        await asyncio.sleep(2)
    raise Exception(f"Timeout waiting for dqlite service {service_name}")


async def _task_is_healthy(task_id: str) -> bool:
    stdout, _, _ = await check_call(
        [
            "docker",
            "inspect",
            "--format",
            "{{ .Status.ContainerStatus.ContainerID }}",
            task_id,
        ]
    )
    if not stdout or not stdout[0]:
        return False
    container_id = stdout[0]
    health_stdout, _, p = await call(
        [
            "docker",
            "inspect",
            "--format",
            "{{ if .State.Health }}{{ .State.Health.Status }}{{ end }}",
            container_id,
        ]
    )
    if p.returncode != 0:
        return False
    status = (health_stdout[0] if health_stdout else "").strip()
    if status != "healthy":
        return False
    _, _, p = await call(
        [
            "docker",
            "exec",
            container_id,
            "nc",
            "-z",
            "-w",
            "2",
            "localhost",
            "10001",
        ]
    )
    return p.returncode == 0


async def bootstrap_first_node(node_name: str) -> None:
    service = dqlite_service_name(node_name)
    await start_dqlite_service(node_name=node_name, peers=None, bootstrap_allowed=True)
    await wait_for_dqlite_service_healthy(service)
    await disable_bootstrap(node_name)
    await wait_for_dqlite_service_healthy(service)
