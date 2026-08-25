from __future__ import annotations

import asyncio
import logging
from asyncio import subprocess

from disco.utils import docker
from disco.utils.cluster_locks import (
    pending_cluster_removes,
    reconciler_lock,
    recovery_lock,
)
from disco.utils.dqlite import (
    cluster_remove,
    disco_name_from_dqlite_service,
    dqlite_bind_address,
    list_dqlite_services,
    remove_dqlite_service,
    start_dqlite_service,
)
from disco.utils.randomname import generate_random_name
from disco.utils.subprocess import call

log = logging.getLogger(__name__)


async def reconcile_dqlite_services() -> None:
    if recovery_lock.locked():
        log.info("Recovery in progress; skipping reconcile")
        return
    if reconciler_lock.locked():
        log.debug("reconcile already in progress, skipping")
        return
    async with reconciler_lock:
        try:
            await _reconcile_once()
        except Exception:
            log.exception("reconcile_dqlite_services failed")


async def _reconcile_once() -> None:
    node_ids = await docker.get_node_list()
    nodes = await docker.get_node_details(node_ids)

    for node in nodes:
        if "disco-name" not in node.labels or not node.labels["disco-name"]:
            new_name = await generate_random_name()
            log.info("Assigning disco-name %s to node %s", new_name, node.id)
            await docker.set_node_label(
                node_id=node.id, key="disco-name", value=new_name
            )
            node.labels["disco-name"] = new_name

    # Down nodes keep their service; a bounced host is not a departure.
    desired_names = {
        node.labels["disco-name"] for node in nodes if node.labels.get("disco-name")
    }
    existing_services = await list_dqlite_services()
    existing_names = {
        n
        for n in (disco_name_from_dqlite_service(s) for s in existing_services)
        if n is not None
    }

    # BOOTSTRAP_ALLOWED is only ever set at init, never here.
    missing = desired_names - existing_names
    for name in missing:
        peers = sorted(dqlite_bind_address(n) for n in existing_names if n != name)
        if not peers:
            log.warning(
                "No existing peers for new node %s; skipping (the first node "
                "is created by init)",
                name,
            )
            continue
        try:
            await start_dqlite_service(
                disco_name=name, peers=peers, bootstrap_allowed=False
            )
            existing_names.add(name)
        except Exception:
            log.exception("Failed to create dqlite service for %s", name)

    orphan = existing_names - desired_names
    for name in orphan:
        log.info("Removing orphan dqlite service for departed node %s", name)
        # Evict before removing the service.
        address = dqlite_bind_address(name)
        evicted = await cluster_remove(address)
        if not evicted:
            # Once the service is gone the orphan check can no longer see this
            # name, so the retry must be queued here.
            pending_cluster_removes.add(address)
            log.info(
                "Cluster .remove for %s deferred",
                name,
            )
        try:
            await remove_dqlite_service(name)
        except Exception:
            log.exception("Failed to remove dqlite service for %s", name)

    for address in list(pending_cluster_removes):
        if await cluster_remove(address):
            pending_cluster_removes.discard(address)
            log.info("Deferred cluster .remove for %s succeeded", address)

    await _sync_caddy_dqlite_nodes(desired_names)


async def _sync_caddy_dqlite_nodes(desired_names: set[str]) -> None:
    # Only update when the value changes: a global-service env update rolls
    # every Caddy task.
    desired = ",".join(sorted(dqlite_bind_address(n) for n in desired_names))
    if not desired:
        return
    stdout, _, p = await call(
        [
            "docker",
            "service",
            "inspect",
            "disco-caddy",
            "--format",
            "{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println .}}{{end}}",
        ]
    )
    if p.returncode != 0:
        return  # Caddy service not present yet.
    current = None
    for line in stdout:
        if line.startswith("DISCO_DQLITE_NODES="):
            current = line[len("DISCO_DQLITE_NODES=") :].strip()
            break
    if current == desired:
        return
    log.info("Updating disco-caddy DISCO_DQLITE_NODES (%d node(s))", len(desired_names))
    _, _, p = await call(
        [
            "docker",
            "service",
            "update",
            "--env-add",
            f"DISCO_DQLITE_NODES={desired}",
            "--detach",
            "disco-caddy",
        ]
    )
    if p.returncode != 0:
        log.warning("Failed to update disco-caddy DISCO_DQLITE_NODES")


async def watch_swarm_events_forever() -> None:
    while True:
        try:
            await _watch_one_stream()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("docker events watcher crashed; restarting in 5s")
        await asyncio.sleep(5)


async def _watch_one_stream() -> None:
    log.info("Starting docker events watcher (type=node)")
    # An unread stderr pipe would fill up and block `docker events`.
    process = await asyncio.create_subprocess_exec(
        "docker",
        "events",
        "--filter",
        "type=node",
        "--format",
        "{{.Type}} {{.Action}} {{.Actor.Attributes}}",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdout is not None
    try:
        # Catch up on events missed while the stream was down.
        await reconcile_dqlite_services()
        async for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            log.info("swarm event: %s", line)
            await reconcile_dqlite_services()
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
