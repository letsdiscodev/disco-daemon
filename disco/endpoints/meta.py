import logging
from enum import Enum
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm.session import Session as DBSession

import disco
from disco.auth import get_api_key
from disco.endpoints.dependencies import get_db
from disco.utils import docker, keyvalues
from disco.utils.meta import update_disco

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key)])


@router.get("/disco/meta")
def meta_get(dbsession: Annotated[DBSession, Depends(get_db)]):
    return {
        "version": disco.__version__,
        "ip": keyvalues.get_value(dbsession, "DISCO_IP"),
        "discoHost": keyvalues.get_value(dbsession, "DISCO_HOST"),
        "registryHost": keyvalues.get_value(dbsession, "REGISTRY_HOST"),
    }


class UpdateRequestBody(BaseModel):
    image: str = Field("letsdiscodev/daemon:latest", pattern=r"^[^-].*$")
    pull: bool = True


@router.post("/disco/upgrade")
def upgrade_post(
    dbsession: Annotated[DBSession, Depends(get_db)], req_body: UpdateRequestBody
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


@router.post("/disco/registry")
def registry_post(
    dbsession: Annotated[DBSession, Depends(get_db)], req_body: SetRegistryRequestBody
):
    disco_host_home = keyvalues.get_value(dbsession, "HOST_HOME")
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
        "ip": keyvalues.get_value(dbsession, "DISCO_IP"),
        "discoHost": keyvalues.get_value(dbsession, "DISCO_HOST"),
        "registryHost": keyvalues.get_value(dbsession, "REGISTRY_HOST"),
    }
