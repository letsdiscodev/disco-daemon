import asyncio
import functools
import logging
import subprocess
import time

from disco.utils.subprocess import call, check_call

log = logging.getLogger(__name__)

DQLITE_IMAGE_TAG = "0.2.0"
DQLITE_PORT = 9001
DQLITE_OVERLAY_NETWORK = "disco-dqlite"


def dqlite_service_name(node_name: str) -> str:
    return f"dqlite-{node_name}"


def dqlite_bind_address(node_name: str) -> str:
    return f"tasks.{dqlite_service_name(node_name)}:{DQLITE_PORT}"


@functools.cache
def get_current_node_name_sync() -> str:
    result = subprocess.run(
        [
            "docker",
            "node",
            "inspect",
            "--format",
            '{{ index .Spec.Labels "disco-name" }}',
            "self",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    name = result.stdout.strip()
    if not name:
        raise RuntimeError("self node has no disco-name label")
    return name



def get_local_dqlite_address() -> str:
    return dqlite_bind_address(get_current_node_name_sync())


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
            "/app/healthcheck.sh",
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
