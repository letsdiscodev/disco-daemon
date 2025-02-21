import logging
from typing import Annotated

import randomname
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from disco.auth import get_api_key_wo_tx
from disco.endpoints.dependencies import get_db
from disco.utils import docker, keyvalues

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])


@router.get("/api/disco/swarm/join-token")
async def join_token_get(dbsession: Annotated[AsyncDBSession, Depends(get_db)]):
    return {
        "joinToken": await docker.get_swarm_join_token(),
        "ip": await keyvalues.get_value(dbsession, "DISCO_ADVERTISE_ADDR"),
        "dockerVersion": await docker.get_docker_version(),
    }


@router.get("/api/disco/swarm/nodes")
async def get_node_list():
    node_ids = await docker.get_node_list()
    nodes = await docker.get_node_details(node_ids)
    for node in nodes:
        if "disco-name" not in node.labels:
            node.labels["disco-name"] = randomname.get_name()
            await docker.set_node_label(
                node_id=node.id, key="disco-name", value=node.labels["disco-name"]
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
    node_ids = await docker.get_node_list()
    nodes = await docker.get_node_details(node_ids)
    for node in nodes:
        if node.labels.get("disco-name") == node_name:
            await docker.remove_node(node_id=node.id)
            return {}
    raise HTTPException(status_code=404)


@router.post("/api/disco/swarm/nodes/{node_name}/drain")
async def node_drain_post(node_name: str):
    node_ids = await docker.get_node_list()
    nodes = await docker.get_node_details(node_ids)
    for node in nodes:
        if node.labels.get("disco-name") == node_name:
            await docker.drain_node(node_id=node.id)
            return {}
    raise HTTPException(status_code=404)
