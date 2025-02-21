import json
import logging
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession
from sse_starlette import EventSourceResponse, ServerSentEvent

import disco
from disco.auth import get_api_key, get_api_key_sync
from disco.endpoints.dependencies import get_db, get_db_sync
from disco.models import ApiKey
from disco.utils import docker, keyvalues
from disco.utils.meta import set_disco_host, update_disco
from disco.utils.projects import get_project_by_domain_sync
from disco.utils.stats import AsyncDockerStats

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_sync)])


@router.get("/api/disco/meta")
async def meta_get(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
):
    return {
        "version": disco.__version__,
        "discoHost": await keyvalues.get_value(dbsession, "DISCO_HOST"),
        "registryHost": await keyvalues.get_value(dbsession, "REGISTRY_HOST"),
        "publicKey": api_key.public_key,
        "docker": {"version": await docker.get_docker_version()},
    }


class UpdateRequestBody(BaseModel):
    image: str = Field("letsdiscodev/daemon:latest", pattern=r"^[^-].*$")
    pull: bool = True


@router.post("/api/disco/upgrade")
def upgrade_post(
    dbsession: Annotated[DBSession, Depends(get_db_sync)], req_body: UpdateRequestBody
):
    update_disco(dbsession=dbsession, image=req_body.image, pull=req_body.pull)
    return {"updating": True}


class RegistryAuthType(Enum):
    basic = "basic"


class SetRegistryRequestBody(BaseModel):
    host: str
    authType: RegistryAuthType
    username: str
    password: str


@router.post("/api/disco/registry")
def registry_post(
    dbsession: Annotated[DBSession, Depends(get_db_sync)],
    req_body: SetRegistryRequestBody,
):
    disco_host_home = keyvalues.get_value_sync(dbsession, "HOST_HOME")
    assert disco_host_home is not None
    docker.login(
        disco_host_home=disco_host_home,
        host=req_body.host,
        username=req_body.username,
        password=req_body.password,
    )
    keyvalues.set_value_sync(
        dbsession=dbsession, key="REGISTRY_HOST", value=req_body.host
    )
    return {
        "version": disco.__version__,
        "discoHost": keyvalues.get_value_sync(dbsession, "DISCO_HOST"),
        "registryHost": keyvalues.get_value_sync(dbsession, "REGISTRY_HOST"),
    }


class SetDiscoHostRequestBody(BaseModel):
    host: str


@router.post("/api/disco/host")
def host_post(
    dbsession: Annotated[DBSession, Depends(get_db_sync)],
    req_body: SetDiscoHostRequestBody,
    api_key: Annotated[ApiKey, Depends(get_api_key_sync)],
):
    project = get_project_by_domain_sync(dbsession, req_body.host)
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

    set_disco_host(dbsession=dbsession, host=req_body.host, by_api_key=api_key)
    return {
        "version": disco.__version__,
        "discoHost": keyvalues.get_value_sync(dbsession, "DISCO_HOST"),
        "registryHost": keyvalues.get_value_sync(dbsession, "REGISTRY_HOST"),
    }


@router.get("/api/disco/stats-experimental")
async def stats_experimental():
    return EventSourceResponse(read_stats())


async def read_stats():
    log.info("Starting stats")
    stats_blah = AsyncDockerStats()
    try:
        while True:
            containers_stats = await stats_blah.get_all_container_stats()
            node_stats = {
                "node_name": "leader",
                "read": datetime.now(timezone.utc).isoformat(),
                "stats": containers_stats,
            }
            yield ServerSentEvent(
                event="stats",
                data=json.dumps(node_stats),
            )
            time.sleep(3)
    finally:
        log.info("Stopping stats")
