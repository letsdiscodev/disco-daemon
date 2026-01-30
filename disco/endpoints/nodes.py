import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from disco.auth import get_api_key_wo_tx
from disco.endpoints.dependencies import get_db
from disco.utils import docker, keyvalues
from disco.utils.dqlite import DQLITE_IMAGE_TAG, disco_name_to_node_id
from disco.utils.randomname import generate_random_name
from disco.utils.subprocess import check_call

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])


async def get_any_existing_dqlite_address() -> str | None:
    """Get the address of any existing dqlite service to join."""
    args = [
        "docker",
        "service",
        "ls",
        "--filter",
        "name=dqlite-",
        "--format",
        "{{ .Name }}",
    ]
    stdout, _, _ = await check_call(args)
    if stdout:
        # Return any existing dqlite service - client will discover leader
        return f"{stdout[0]}:9001"
    return None


async def start_dqlite_for_node(disco_name: str) -> None:
    """Start a dqlite service on a newly joined node."""
    service_name = f"dqlite-{disco_name}"

    # Check if already running
    if await docker.service_exists(service_name):
        log.info("dqlite service %s already exists", service_name)
        return

    # Find an existing dqlite service to join
    join_address = await get_any_existing_dqlite_address()
    if not join_address:
        raise RuntimeError("No existing dqlite cluster to join")

    node_id = disco_name_to_node_id(disco_name)

    log.info(
        "Starting dqlite service %s with NODE_ID %d, joining %s",
        service_name,
        node_id,
        join_address,
    )
    args = [
        "docker",
        "service",
        "create",
        "--name",
        service_name,
        "--network",
        "disco-dqlite",
        "--network",
        "disco-main",
        "--constraint",
        f"node.labels.disco-name=={disco_name}",
        "--mount",
        f"source=dqlite-{disco_name},target=/data",
        "--env",
        f"NODE_ID={node_id}",
        "--env",
        "PORT=9001",
        "--env",
        f"JOIN={join_address}",
        "--health-cmd",
        "/app/healthcheck.sh",
        "--health-interval",
        "5s",
        "--health-start-period",
        "30s",
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=20m",
        f"letsdiscodev/dqlite:{DQLITE_IMAGE_TAG}",
    ]
    await check_call(args)


@router.get("/api/disco/swarm/join-token")
async def join_token_get(dbsession: Annotated[AsyncDBSession, Depends(get_db)]):
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
    for node in nodes:
        if "disco-name" not in node.labels:
            # New node detected - assign disco-name and start dqlite
            node.labels["disco-name"] = await generate_random_name()
            await docker.set_node_label(
                node_id=node.id, key="disco-name", value=node.labels["disco-name"]
            )
            log.info(
                "New node %s assigned disco-name: %s",
                node.id,
                node.labels["disco-name"],
            )
            # Start dqlite service for this node
            try:
                await start_dqlite_for_node(node.labels["disco-name"])
            except Exception as e:
                log.error(
                    "Failed to start dqlite for node %s: %s",
                    node.labels["disco-name"],
                    e,
                )
    return {
        "nodes": [
            {
                "created": node.created,
                "name": node.labels["disco-name"],
                "state": node.state,
                "address": node.address,
                "isLeader": node.labels.get("disco-role") == "main",
            }
            for node in nodes
        ],
    }


@router.delete("/api/disco/swarm/nodes/{node_name}")
async def node_delete(node_name: str):
    log.info("Removing node %s", node_name)
    node_ids = await docker.get_node_list()
    nodes = await docker.get_node_details(node_ids)
    node_id = None
    for node in nodes:
        if node.labels.get("disco-name") == node_name:
            if node.labels.get("disco-role") == "main":
                raise HTTPException(422, "Can't remove main node")
            node_id = node.id
    if node_id is None:
        log.info("Didn't find node %s", node_name)
        raise HTTPException(status_code=404)

    # Remove dqlite service for this node
    dqlite_service = f"dqlite-{node_name}"
    if await docker.service_exists(dqlite_service):
        log.info("Removing dqlite service %s", dqlite_service)
        await docker.rm_service(dqlite_service)

    log.info("Starting swarm leaver job for node %s", node_name)
    service_name = await docker.leave_swarm(node_id=node_id)
    log.info("Draining node %s", node_name)
    await docker.drain_node(node_id=node_id)
    log.info("Removing swarm leaver service for node %s", node_name)
    await docker.rm_service(service_name)
    timeout = datetime.now(timezone.utc) + timedelta(minutes=20)
    while datetime.now(timezone.utc) < timeout:
        try:
            log.info("Removing node %s", node_name)
            await docker.remove_node(node_id=node_id)
            log.info("Removed node %s", node_name)
            return {}
        except Exception:
            log.info("Failed to remove, node, waiting 5 seconds")
            await asyncio.sleep(5)
    log.info("Removing node --force %s", node_name)
    await docker.remove_node(node_id=node_id, force=True)
    log.info("Removed node --force %s", node_name)
    return {}
