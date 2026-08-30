import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from sse_starlette import EventSourceResponse, ServerSentEvent

import disco
from disco.auth import get_api_key_wo_tx
from disco.models.db import ReadSession, Session
from disco.utils import docker, keyvalues
from disco.utils.apikeys import get_valid_api_key_by_id
from disco.utils.meta import set_disco_host, update_disco
from disco.utils.projectdomains import DOMAIN_REGEX
from disco.utils.projects import get_project_by_domain
from disco.utils.stats import DockerStats

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])


@router.get("/api/disco/meta")
async def meta_get(
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
):
    async with ReadSession.begin() as dbsession:
        api_key = await get_valid_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        return {
            "version": disco.__version__,
            "discoHost": await keyvalues.get_value(dbsession, "DISCO_HOST"),
            "registry": await keyvalues.get_value(dbsession, "REGISTRY"),
            # registryHost for backward compat, remove after 2027-02-01
            "registryHost": await keyvalues.get_value(dbsession, "REGISTRY"),
            "publicKey": api_key.public_key,
            "docker": {"version": await docker.get_docker_version()},
        }


class UpdateRequestBody(BaseModel):
    image: str = Field("letsdiscodev/daemon:latest", pattern=r"^[^-].*$")
    pull: bool = True


@router.post("/api/disco/upgrade")
async def upgrade_post(req_body: UpdateRequestBody):
    async with Session.begin() as dbsession:
        await update_disco(
            dbsession=dbsession, image=req_body.image, pull=req_body.pull
        )
    return {"updating": True}


class SetDiscoHostRequestBody(BaseModel):
    host: str = Field(..., pattern=DOMAIN_REGEX)


@router.post("/api/disco/host")
async def host_post(
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    req_body: SetDiscoHostRequestBody,
):
    async with Session.begin() as dbsession:
        api_key = await get_valid_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        project = await get_project_by_domain(dbsession, req_body.host)
        if project is not None:
            raise RequestValidationError(
                errors=(
                    ValidationError.from_exception_data(
                        "ValueError",
                        [
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "value_error",
                                    "Domain already taken by other project",
                                ),
                                loc=("body", "domain"),
                                input=req_body.host,
                            )
                        ],
                    )
                ).errors()
            )
        await set_disco_host(
            dbsession=dbsession, host=req_body.host, by_api_key=api_key
        )
        return {
            "version": disco.__version__,
            "discoHost": await keyvalues.get_value_str(dbsession, "DISCO_HOST"),
            "registry": await keyvalues.get_value(dbsession, "REGISTRY"),
            # registryHost for backward compat, remove after 2027-02-01
            "registryHost": await keyvalues.get_value(dbsession, "REGISTRY"),
        }


@router.get("/api/disco/stats-experimental")
async def stats_experimental():
    return EventSourceResponse(read_stats())


async def read_stats():
    log.info("Starting stats")
    async_docker_stats = DockerStats()
    try:
        while True:
            containers_stats = await async_docker_stats.get_all_container_stats()
            df = await docker.host_df()
            node_stats = {
                "node_name": "leader",
                "read": datetime.now(timezone.utc).isoformat(),
                "stats": containers_stats,
                "df": {
                    "used": df.used,
                    "available": df.available,
                },
            }
            yield ServerSentEvent(
                event="stats",
                data=json.dumps(node_stats),
            )
            await asyncio.sleep(3)
    finally:
        log.info("Stopping stats")
