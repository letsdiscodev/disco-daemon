"""dqlite cluster management utilities.

Per-node Swarm service model: each Swarm node hosts a single-replica
service named `dqlite-<disco-name>`, pinned to that node via a node-label
constraint. Each service is on the `disco-dqlite` overlay network with
`--endpoint-mode dnsrr`. Tasks advertise `tasks.dqlite-<disco-name>:9001`
as their bind address — a stable Swarm DNS name that resolves to whatever
the current task's overlay IP is. The dqlite image's LD_PRELOAD shim
enables hostname-style bind addresses in dqlite-demo.
"""

import asyncio
import functools
import logging

from disco.utils.subprocess import call, check_call

log = logging.getLogger(__name__)

DQLITE_IMAGE_TAG = "0.2.0-dns"
DQLITE_PORT = 9001
DQLITE_OVERLAY_NETWORK = "disco-dqlite"


def dqlite_service_name(disco_name: str) -> str:
    """Per-node Swarm service name for a given disco-name."""
    return f"dqlite-{disco_name}"


def dqlite_bind_address(disco_name: str) -> str:
    """Stable DNS bind address used by dqlite for this node.

    Resolves to the per-node service's single task IP via Swarm DNS.
    Used both for the node's own --db flag and as a PEERS entry on others.
    """
    return f"tasks.{dqlite_service_name(disco_name)}:{DQLITE_PORT}"


def disco_name_from_dqlite_service(service_name: str) -> str | None:
    """Inverse of dqlite_service_name. Returns None for non-matching names."""
    prefix = "dqlite-"
    if not service_name.startswith(prefix):
        return None
    return service_name[len(prefix):]


@functools.cache
def get_current_node_disco_name_sync() -> str:
    """Synchronous variant for use during init (before async loop is up)."""
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
    """Async variant; the disco-name is stable across the daemon's lifetime."""
    return await asyncio.to_thread(get_current_node_disco_name_sync)


def get_local_dqlite_address() -> str:
    """The DNS address of the dqlite running on this node.

    Used by SQLAlchemy to connect to dqlite. Always points at the local
    per-node service (tasks.dqlite-<self>) which Swarm DNS resolves to
    the single task running on this same node.
    """
    return dqlite_bind_address(get_current_node_disco_name_sync())


async def list_dqlite_services() -> list[str]:
    """Names of all dqlite-* services in the swarm."""
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
    """Container ID of the running task for a service, if any.

    docker service ps shows tasks across all nodes; we filter to running.
    The container ID can then be used with `docker exec` against the
    manager's Docker socket (Swarm forwards exec to the right node).
    """
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
    """Container ID of a dqlite container on the current node, if any.

    Used as a foothold for executing the dqlite admin CLI: we can exec
    .remove / .cluster / .leader via this container without crossing nodes.
    """
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
    """Create the per-node dqlite Swarm service.

    `peers` is a list of bind addresses (`tasks.dqlite-X:9001`) the new
    node should try to join via. Pass an empty list (or None) only on the
    very first node, with bootstrap_allowed=True.
    """
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
    """Remove BOOTSTRAP_ALLOWED from the service spec.

    Triggers a rolling update — the task is replaced with a new container
    at a fresh overlay IP, but the persisted bind address is a DNS name
    that's still valid. This is called once, immediately after the very
    first task is healthy on install.
    """
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
    """Remove the per-node Swarm service AND its named volume.

    Best-effort: tolerates missing service / volume. Volume removal only
    succeeds if no container still references it, which is normally the
    case after `service rm`.
    """
    service_name = dqlite_service_name(disco_name)
    volume_name = f"dqlite-{disco_name}"
    log.info("Removing dqlite service %s", service_name)
    _, _, p = await call(["docker", "service", "rm", service_name])
    if p.returncode != 0:
        log.info("service rm %s returned %d (probably already gone)", service_name, p.returncode)
    # Volume lives on the node — removing it from the manager only works
    # if the node is still in the swarm AND the volume is unreferenced.
    # Either failure mode is non-fatal here.
    _, _, p = await call(["docker", "volume", "rm", volume_name])
    if p.returncode != 0:
        log.info("volume rm %s returned %d (left behind on host)", volume_name, p.returncode)


async def wait_for_dqlite_service_healthy(
    service_name: str, timeout_seconds: int = 180
) -> None:
    """Block until the service has a Running task and its API port is open."""
    log.info("Waiting for %s to become healthy", service_name)
    import time

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
                "{{ .CurrentState }}",
            ]
        )
        if any(state.startswith("Running") for state in stdout):
            log.info("%s has a Running task", service_name)
            return
        await asyncio.sleep(2)
    raise Exception(f"Timeout waiting for dqlite service {service_name}")


def wait_for_dqlite_service_healthy_sync(
    service_name: str, timeout_seconds: int = 180
) -> None:
    """Sync wrapper for init script use."""
    asyncio.run(wait_for_dqlite_service_healthy(service_name, timeout_seconds))


async def query_cluster_members() -> list[dict] | None:
    """Run `.cluster` against a local dqlite container and parse the output.

    Each row of `.cluster` output is `<id>|<address>|<role>` where role
    is one of voter/standby/spare. Returns None if no local container is
    available to query (e.g. local dqlite is itself down).
    """
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
        # Strip the "dqlite> " prompt prefix that the CLI sometimes adds.
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
    """Evict a node from the dqlite cluster by its advertised address.

    Called when a Swarm node is decommissioned, so role-management
    doesn't keep the dead node as a phantom voter. Returns False if
    no local dqlite container is reachable to issue the command —
    caller should retry later (the reconciler will).
    """
    container = await any_running_dqlite_container_on_this_node()
    if container is None:
        log.warning(
            "No local dqlite container to issue .remove via; will retry later"
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
    """Rewrite a node's local raft state so the cluster has only this node.

    Runs as a one-shot container that mounts the node's dqlite volume.
    The caller must scale the dqlite service to 0 first so the data dir
    is not in use. After this returns, scaling the service back to 1
    starts dqlite at the new bind/membership.

    The container also takes a defensive `cp -a` snapshot of the data
    dir to `<dir>.backup-<ts>/` inside the volume, in case the recovery
    needs manual unwinding later.

    Only meaningful when called against the disco-daemon's local node —
    docker volumes are node-local, so this won't reach other hosts.
    """
    bind = dqlite_bind_address(disco_name)
    volume = f"dqlite-{disco_name}"
    # The script that runs inside the dqlite image. Care with shell
    # quoting: the f-string interpolates `bind` (which contains colons
    # but not shell metacharacters in our addresses) and that's it.
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
        'NODE_ID=$(awk -F": " "/^ID:/ {print \\$2; exit}" "$DATA_DIR/info.yaml")\n'
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
    """Erase the dqlite-<disco_name> volume's contents on its host.

    Uses a replicated-job constrained to the target node, because the
    volume lives on that node's host and can't be reached directly from
    the daemon's container.
    """
    job_name = f"disco-wipe-dqlite-{disco_name}-{int(asyncio.get_event_loop().time() * 1000) % 1_000_000}"
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
    """Wait for a replicated-job's tasks to reach Complete state."""
    import time

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
    """Bootstrap-create the very first dqlite service (single-node cluster).

    Called from the init script, which runs in its own process and creates
    the daemon's database. Subsequent nodes are managed by the reconciler.
    """
    asyncio.run(
        start_dqlite_service(disco_name=disco_name, peers=None, bootstrap_allowed=True)
    )


def strip_bootstrap_allowed_sync(disco_name: str) -> None:
    asyncio.run(strip_bootstrap_allowed(disco_name))
