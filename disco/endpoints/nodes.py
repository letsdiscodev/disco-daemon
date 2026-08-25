import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException

from disco import config
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
from disco.utils.randomname import generate_random_name
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
    node_ids = await docker.get_node_list()
    nodes = await docker.get_node_details(node_ids)
    if not config.is_dqlite_mode():
        for node in nodes:
            if "disco-name" not in node.labels:
                node.labels["disco-name"] = await generate_random_name()
                await docker.set_node_label(
                    node_id=node.id, key="disco-name", value=node.labels["disco-name"]
                )
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
                # "unknown": manager hasn't heard from the node yet, treat as down
                "isDown": node.state in ("down", "disconnected", "unknown"),
            }
            for node in nodes
        ],
    }


@router.delete("/api/disco/swarm/nodes/{node_name}")
async def node_delete(node_name: str):
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
    if config.is_dqlite_mode():
        # Keep a periodic reconcile from recreating the service mid-teardown.
        async with reconciler_lock:
            await _do_node_delete(target, node_name, node_is_down)

        await reconcile_dqlite_services()
    else:
        await _do_node_delete(target, node_name, node_is_down)
    return {}


async def _do_node_delete(target, node_name: str, node_is_down: bool) -> None:
    if node_is_down:
        log.info(
            "Node %s is %s; using forced removal path", node_name, target.state
        )
        if config.is_dqlite_mode():
            # Evict from the raft cluster before removing the service; once
            # the service is gone the reconciler can't detect the phantom voter.
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
        # docker node rm can lag after the node leaves; wait, then force.
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
