import asyncio
import functools
import logging
import time
import uuid

from disco.utils.subprocess import call, check_call

log = logging.getLogger(__name__)

DQLITE_IMAGE_TAG = "0.2.0"
DQLITE_PORT = 9001
DQLITE_OVERLAY_NETWORK = "disco-dqlite"


def dqlite_service_name(disco_name: str) -> str:
    return f"dqlite-{disco_name}"


def dqlite_bind_address(disco_name: str) -> str:
    # Swarm DNS name of the node's single task; the dqlite image's LD_PRELOAD
    # shim lets dqlite-demo bind to a hostname.
    return f"tasks.{dqlite_service_name(disco_name)}:{DQLITE_PORT}"


def disco_name_from_dqlite_service(service_name: str) -> str | None:
    prefix = "dqlite-"
    if not service_name.startswith(prefix):
        return None
    return service_name[len(prefix):]


@functools.cache
def get_current_node_disco_name_sync() -> str:
    import subprocess

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


async def get_current_node_disco_name() -> str:
    return await asyncio.to_thread(get_current_node_disco_name_sync)


def get_local_dqlite_address() -> str:
    return dqlite_bind_address(get_current_node_disco_name_sync())


async def list_dqlite_services() -> list[str]:
    stdout, _, _ = await check_call(
        [
            "docker",
            "service",
            "ls",
            "--filter",
            "name=dqlite-",
            "--format",
            "{{ .Name }}",
        ]
    )
    return [s for s in stdout if disco_name_from_dqlite_service(s) is not None]


async def dqlite_service_running_task_id(service_name: str) -> str | None:
    stdout, _, _ = await check_call(
        [
            "docker",
            "service",
            "ps",
            service_name,
            "--filter",
            "desired-state=running",
            "--format",
            "{{ .ID }}",
        ]
    )
    if not stdout:
        return None
    return stdout[0]


async def any_running_dqlite_container_on_this_node() -> str | None:
    stdout, _, _ = await check_call(
        [
            "docker",
            "ps",
            "-q",
            "--filter",
            "name=dqlite-",
            "--filter",
            "status=running",
        ]
    )
    if not stdout:
        return None
    return stdout[0]


async def start_dqlite_service(
    *,
    disco_name: str,
    peers: list[str] | None,
    bootstrap_allowed: bool,
) -> None:
    service_name = dqlite_service_name(disco_name)
    bind = dqlite_bind_address(disco_name)
    env_args: list[str] = ["--env", f"BIND_ADDRESS={bind}"]
    if bootstrap_allowed:
        env_args += ["--env", "BOOTSTRAP_ALLOWED=true"]
    if peers:
        env_args += ["--env", "PEERS=" + ",".join(peers)]
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
            f"node.labels.disco-name=={disco_name}",
            "--mount",
            f"source=dqlite-{disco_name},target=/data",
            "--label",
            "disco.service=dqlite",
            "--label",
            f"disco.dqlite.disco-name={disco_name}",
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


async def strip_bootstrap_allowed(disco_name: str) -> None:
    service_name = dqlite_service_name(disco_name)
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


async def remove_dqlite_service(disco_name: str) -> None:
    service_name = dqlite_service_name(disco_name)
    volume_name = f"dqlite-{disco_name}"
    log.info("Removing dqlite service %s", service_name)
    _, _, p = await call(["docker", "service", "rm", service_name])
    if p.returncode != 0:
        log.info("service rm %s returned %d", service_name, p.returncode)
    _, _, p = await call(["docker", "volume", "rm", volume_name])
    if p.returncode != 0:
        log.info("volume rm %s returned %d", volume_name, p.returncode)


async def wait_for_dqlite_service_healthy(
    service_name: str, timeout_seconds: int = 180
) -> None:
    # A Running task is not enough: within the health start period dqlite-demo
    # may not have opened its API port yet.
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
        ["docker", "inspect", "--format",
         "{{ .Status.ContainerStatus.ContainerID }}", task_id]
    )
    if not stdout or not stdout[0]:
        return False
    container_id = stdout[0]
    health_stdout, _, p = await call(
        ["docker", "inspect", "--format",
         "{{ if .State.Health }}{{ .State.Health.Status }}{{ end }}",
         container_id]
    )
    if p.returncode != 0:
        return False
    status = (health_stdout[0] if health_stdout else "").strip()
    # No healthcheck configured: probe the API port, which only binds once
    # dqlite-demo has finished initializing.
    if status == "":
        _, _, p = await call(
            ["docker", "exec", container_id, "nc", "-z", "-w", "2",
             "localhost", "10001"]
        )
        return p.returncode == 0
    return status == "healthy"


async def wait_for_service_tasks_stopped(
    service_name: str, timeout_seconds: int = 60
) -> None:
    # `docker service scale --detach` returns before the tasks have stopped and
    # released their volumes.
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        stdout, _, _ = await check_call(
            ["docker", "service", "ps", service_name,
             "--filter", "desired-state=running",
             "--format", "{{ .CurrentState }}"]
        )
        if not any(state.startswith("Running") or state.startswith("Starting")
                   or state.startswith("Preparing") or state.startswith("Ready")
                   for state in stdout):
            return
        await asyncio.sleep(1)
    raise Exception(
        f"Timeout waiting for tasks of {service_name} to stop"
    )


def wait_for_dqlite_service_healthy_sync(
    service_name: str, timeout_seconds: int = 180
) -> None:
    asyncio.run(wait_for_dqlite_service_healthy(service_name, timeout_seconds))


async def query_cluster_members() -> list[dict] | None:
    container = await any_running_dqlite_container_on_this_node()
    if container is None:
        return None
    local_bind = get_local_dqlite_address()
    stdout, _, p = await call(
        [
            "docker",
            "exec",
            "-i",
            container,
            "/app/dqlite",
            "-s",
            local_bind,
            "demo",
        ],
        stdin=".cluster\n",
    )
    if p.returncode != 0:
        return None
    members: list[dict] = []
    for line in stdout:
        if line.startswith("dqlite> "):
            line = line[len("dqlite> "):]
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) != 3:
            continue
        node_id, address, role = parts
        members.append({"id": node_id, "address": address, "role": role})
    return members


async def cluster_remove(address: str) -> bool:
    container = await any_running_dqlite_container_on_this_node()
    if container is None:
        log.warning(
            "No local dqlite container to issue .remove via"
        )
        return False
    log.info("Evicting %s from dqlite cluster via container %s", address, container)
    local_bind = get_local_dqlite_address()
    _, stderr, p = await call(
        [
            "docker",
            "exec",
            "-i",
            container,
            "/app/dqlite",
            "-s",
            local_bind,
            "demo",
        ],
        stdin=f".remove {address}\n",
    )
    if p.returncode != 0:
        log.warning(".remove %s failed: %s", address, stderr)
        return False
    return True


async def reconfigure_node_as_single_member(disco_name: str) -> None:
    # Caller must scale the service to 0 first. Volumes are node-local, so this
    # only works for the daemon's own node.
    bind = dqlite_bind_address(disco_name)
    volume = f"dqlite-{disco_name}"
    # Both `.reconfigure` and copying cluster.yaml are required: `.reconfigure`
    # rewrites the raft state but not cluster.yaml, which dqlite-demo reads at
    # startup.
    script = (
        "set -eu\n"
        f"BIND='{bind}'\n"
        'DATA_DIR="/data/$BIND"\n'
        'if [ ! -f "$DATA_DIR/info.yaml" ]; then\n'
        '  echo "ERROR: no info.yaml at $DATA_DIR" >&2\n'
        "  exit 1\n"
        "fi\n"
        'TS=$(date -u +%Y%m%dT%H%M%SZ)\n'
        'BACKUP_DIR="$DATA_DIR.backup-$TS"\n'
        'cp -a "$DATA_DIR" "$BACKUP_DIR"\n'
        'NODE_ID=$(awk "/^ID:/ { sub(/^ID:[ \\t]*/, \\"\\"); print; exit }" '
        '"$DATA_DIR/info.yaml")\n'
        'if [ -z "$NODE_ID" ]; then\n'
        '  echo "ERROR: could not parse NODE_ID from $DATA_DIR/info.yaml" >&2\n'
        "  exit 2\n"
        "fi\n"
        'cat > /tmp/new.yaml <<EOF\n'
        '- ID: $NODE_ID\n'
        '  Address: $BIND\n'
        '  Role: 0\n'
        'EOF\n'
        'echo ".reconfigure $DATA_DIR /tmp/new.yaml" | /app/dqlite -s ignored demo\n'
        'cp /tmp/new.yaml "$DATA_DIR/cluster.yaml"\n'
        'echo "Reconfigured $BIND as single-node cluster (backup at $BACKUP_DIR)"\n'
    )
    args = [
        "docker", "run", "--rm",
        "--mount", f"type=volume,source={volume},target=/data",
        f"letsdiscodev/dqlite:{DQLITE_IMAGE_TAG}",
        "sh", "-c", script,
    ]
    log.info("Reconfiguring %s as sole cluster member", disco_name)
    await check_call(args)


async def wipe_dqlite_volume_via_job(disco_name: str, timeout_seconds: int = 60) -> None:
    # The volume is node-local, so run a job constrained to that node.
    job_name = f"disco-wipe-dqlite-{disco_name}-{uuid.uuid4().hex[:8]}"
    args = [
        "docker", "service", "create",
        "--name", job_name,
        "--mode", "replicated-job",
        "--restart-condition", "none",
        "--constraint", f"node.labels.disco-name=={disco_name}",
        "--mount", f"type=volume,source=dqlite-{disco_name},target=/data",
        "busybox",
        "sh", "-c", "rm -rf /data/* /data/.[!.]* 2>/dev/null; echo done",
    ]
    log.info("Wiping dqlite volume on %s via job %s", disco_name, job_name)
    try:
        await check_call(args)
        await _wait_for_job_complete(job_name, timeout_seconds=timeout_seconds)
    finally:
        _, _, _ = await call(["docker", "service", "rm", job_name])


async def _wait_for_job_complete(job_name: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        stdout, _, _ = await check_call([
            "docker", "service", "ps", job_name,
            "--format", "{{.CurrentState}}",
        ])
        if stdout:
            if any(s.startswith("Failed") or s.startswith("Rejected") for s in stdout):
                raise Exception(f"job {job_name} failed: {stdout}")
            if all(s.startswith("Complete") or s.startswith("Shutdown") for s in stdout):
                return
        await asyncio.sleep(2)
    raise Exception(f"job {job_name} did not complete within {timeout_seconds}s")


async def scale_dqlite_service(disco_name: str, replicas: int) -> None:
    service = dqlite_service_name(disco_name)
    await check_call([
        "docker", "service", "scale", f"{service}={replicas}", "--detach",
    ])


def start_first_dqlite_service_sync(disco_name: str) -> None:
    asyncio.run(
        start_dqlite_service(disco_name=disco_name, peers=None, bootstrap_allowed=True)
    )


def strip_bootstrap_allowed_sync(disco_name: str) -> None:
    asyncio.run(strip_bootstrap_allowed(disco_name))
