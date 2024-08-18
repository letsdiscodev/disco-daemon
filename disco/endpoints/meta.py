import logging
from enum import Enum
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from sqlalchemy.orm.session import Session as DBSession

import disco
from disco.auth import get_api_key_sync
from disco.endpoints.dependencies import get_sync_db
from disco.models import ApiKey
from disco.utils import docker, keyvalues
from disco.utils.meta import set_disco_host, update_disco
from disco.utils.projects import get_project_by_domain_sync

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_sync)])


@router.get("/api/disco/meta")
def meta_get(
    dbsession: Annotated[DBSession, Depends(get_sync_db)],
    api_key: Annotated[ApiKey, Depends(get_api_key_sync)],
):
    return {
        "version": disco.__version__,
        "discoHost": keyvalues.get_value_sync(dbsession, "DISCO_HOST"),
        "registryHost": keyvalues.get_value_sync(dbsession, "REGISTRY_HOST"),
        "publicKey": api_key.public_key,
    }


class UpdateRequestBody(BaseModel):
    image: str = Field("letsdiscodev/daemon:latest", pattern=r"^[^-].*$")
    pull: bool = True


@router.post("/api/disco/upgrade")
def upgrade_post(
    dbsession: Annotated[DBSession, Depends(get_sync_db)], req_body: UpdateRequestBody
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
    dbsession: Annotated[DBSession, Depends(get_sync_db)],
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
    keyvalues.set_value(dbsession=dbsession, key="REGISTRY_HOST", value=req_body.host)
    return {
        "version": disco.__version__,
        "discoHost": keyvalues.get_value_sync(dbsession, "DISCO_HOST"),
        "registryHost": keyvalues.get_value_sync(dbsession, "REGISTRY_HOST"),
    }


class SetDiscoHostRequestBody(BaseModel):
    host: str


@router.post("/api/disco/host")
def host_post(
    dbsession: Annotated[DBSession, Depends(get_sync_db)],
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
