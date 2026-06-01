import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from disco.auth import get_api_key_wo_tx
from disco.models.db import AsyncReadSession
from disco.utils import docker, keyvalues
from disco.utils.cluster_locks import (
    pending_cluster_removes,
    reconciler_lock,
)
from disco.utils.dqlite import (
    cluster_remove,
    dqlite_bind_address,
    remove_dqlite_service,
)
from disco.utils.swarmreconciler import reconcile_dqlite_services

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])


@router.get("/api/disco/swarm/join-token")
async def join_token_get():
    async with AsyncReadSession.begin() as dbsession:
        return {
            "joinToken": await docker.get_swarm_join_token(),
            "ip": await keyvalues.get_value(dbsession, "DISCO_ADVERTISE_ADDR"),
            "dockerVersion": await docker.get_docker_version(),
            "registry": await keyvalues.get_value(dbsession, "REGISTRY"),
            # registryHost for backward compat, remove after 2027-02-01
            "registryHost": await keyvalues.get_value(dbsession, "REGISTRY"),
        }


@router.get("/api/disco/swarm/nodes")
async def get_node_list():
    """Snapshot of the current Swarm membership.

    This is a *Swarm-side* view, so it works even when one or more nodes
    are unreachable — we read everything from the manager's local Swarm
    raft state via `docker node inspect`, not from the nodes themselves.

    The new disco-name assignment (for nodes that don't yet have a label)
    and per-node dqlite service creation are owned by the reconciler now,
    not this endpoint. We just describe what's there.
    """
    node_ids = await docker.get_node_list()
    nodes = await docker.get_node_details(node_ids)
    return {
        "nodes": [
            {
                "created": node.created,
                "name": node.labels.get("disco-name") or "",
                "state": node.state,
                "availability": node.availability,
                "role": node.role,
                "address": node.address,
                "isLeader": node.labels.get("disco-role") == "main",
                "isReady": node.state == "ready",
                # "unknown" can happen when a manager hasn't heard from a
                # node yet (e.g. just-rebooted host). Treat it as down so
                # the down-tolerant DELETE path doesn't try to ssh into
                # an unreachable node.
                "isDown": node.state in ("down", "disconnected", "unknown"),
            }
            for node in nodes
        ],
    }


@router.delete("/api/disco/swarm/nodes/{node_name}")
async def node_delete(node_name: str):
    """Remove a node from the Swarm and clean up its dqlite footprint.

    Works whether the target node is reachable or not. For unreachable
    nodes we skip the graceful "tell the node to leave" steps (which
    would hang) and go straight to forced removal on the manager side.
    The dqlite service + raft membership cleanup happens via the
    reconciler at the end — that codepath is the same for both flows
    and is idempotent, so a partial failure just gets retried by the
    periodic reconcile.
    """
    log.info("Removing node %s", node_name)
    node_ids = await docker.get_node_list()
    nodes = await docker.get_node_details(node_ids)
    target = None
    for node in nodes:
        if node.labels.get("disco-name") == node_name:
            target = node
            break
    if target is None:
        log.info("Didn't find node %s", node_name)
        raise HTTPException(status_code=404)
    if target.labels.get("disco-role") == "main":
        raise HTTPException(422, "Can't remove main node")

    node_is_down = target.state in ("down", "disconnected", "unknown")
    # Hold the reconciler lock through the mutating section so a periodic
    # reconcile can't race with us (e.g. recreating the dqlite service
    # we're in the middle of tearing down).
    async with reconciler_lock:
        await _do_node_delete(target, node_name, node_is_down)

    # Final reconciler pass: removes the per-node dqlite service if it's
    # still around and ensures cluster .remove was called (idempotent).
    await reconcile_dqlite_services()
    return {}


async def _do_node_delete(target, node_name: str, node_is_down: bool) -> None:
    if node_is_down:
        log.info(
            "Node %s is %s; using forced removal path", node_name, target.state
        )
        # Try to evict the dqlite address proactively before tearing the
        # service down — cluster_remove uses a local container so it
        # doesn't need the dead node to be reachable. If our local
        # container is itself momentarily down (e.g. mid-restart),
        # queue the address for the reconciler to retry; without that,
        # the phantom voter would persist indefinitely because once the
        # service is removed the reconciler can no longer detect it.
        address = dqlite_bind_address(node_name)
        if not await cluster_remove(address):
            pending_cluster_removes.add(address)
            log.warning(
                "cluster_remove(%s) deferred; reconciler will retry", address
            )
        await remove_dqlite_service(node_name)
        try:
            await docker.remove_node(node_id=target.id, force=True)
        except Exception:
            log.exception("force docker node rm %s failed", node_name)
            raise HTTPException(500, f"Failed to force-remove node {node_name}")
    else:
        log.info("Starting swarm leaver job for node %s", node_name)
        leaver_service = await docker.leave_swarm(node_id=target.id)
        log.info("Draining node %s", node_name)
        await docker.drain_node(node_id=target.id)
        log.info("Removing swarm leaver service for node %s", node_name)
        await docker.rm_service(leaver_service)
        # Once the node has left the swarm, docker node rm will succeed.
        # It can lag — wait for up to 20 minutes, then force.
        deadline = datetime.now(timezone.utc) + timedelta(minutes=20)
        removed = False
        while datetime.now(timezone.utc) < deadline:
            try:
                await docker.remove_node(node_id=target.id)
                log.info("Removed node %s", node_name)
                removed = True
                break
            except Exception:
                log.info("docker node rm failed, retrying in 5s")
                await asyncio.sleep(5)
        if not removed:
            log.info("Force-removing node %s after timeout", node_name)
            await docker.remove_node(node_id=target.id, force=True)
