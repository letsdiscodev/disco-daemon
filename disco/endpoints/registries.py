import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

import disco
from disco.auth import get_api_key, get_api_key_wo_tx
from disco.endpoints.dependencies import get_db
from disco.models import ApiKey
from disco.models.db import AsyncSession
from disco.utils import docker, keyvalues
from disco.utils.apikeys import get_valid_api_key_by_id

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])


@router.get(
    "/api/disco/registries", status_code=200, dependencies=[Depends(get_api_key_wo_tx)]
)
async def registries_get():
    async with AsyncSession.begin() as dbsession:
        disco_host_home = await keyvalues.get_value(dbsession, "HOST_HOME")
        assert disco_host_home is not None

    registries = await docker.get_authenticated_registries(disco_host_home)
    return {
        "registries": [
            {
                "address": registry,
            }
            for registry in registries
        ],
    }


class LoginRequestBody(BaseModel):
    address: str
    username: str
    password: str


@router.post("/api/disco/registries", status_code=200)
async def registries_post(
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    req_body: LoginRequestBody,
):
    async with AsyncSession.begin() as dbsession:
        api_key = await get_valid_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        log.info(
            "%s is attempting to log into Docker registry %s",
            api_key.log(),
            req_body.address,
        )
        disco_host_home = await keyvalues.get_value(dbsession, "HOST_HOME")
        assert disco_host_home is not None

    try:
        await docker.login(
            disco_host_home=disco_host_home,
            address=req_body.address,
            username=req_body.username,
            password=req_body.password,
        )
    except Exception:
        log.info(
            "%s login attempt failed for Docker registry %s",
            api_key.log(),
            req_body.address,
        )
        raise HTTPException(422, "Login failed")
    registries = await docker.get_authenticated_registries(disco_host_home)
    return {
        "registries": [
            {
                "address": registry,
            }
            for registry in registries
        ],
    }


@router.delete("/api/disco/registries/{registry_address}", status_code=200)
async def registries_logout(
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    registry_address: str,
):
    async with AsyncSession.begin() as dbsession:
        api_key = await get_valid_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        log.info(
            "%s is logging out from Docker registry %s", api_key.log(), registry_address
        )
        disco_host_home = await keyvalues.get_value(dbsession, "HOST_HOME")
        assert disco_host_home is not None
    try:
        await docker.logout(
            disco_host_home=disco_host_home,
            address=registry_address,
        )
    except Exception:
        log.info(
            "%s logout attempt failed for Docker registry %s",
            api_key.log(),
            registry_address,
        )
        raise HTTPException(422, "Logout failed")
    registries = await docker.get_authenticated_registries(disco_host_home)
    return {
        "registries": [
            {
                "address": registry,
            }
            for registry in registries
        ],
    }


class SetRegistryRequestBody(BaseModel):
    address: str


@router.post("/api/disco/registry")
async def registry_post(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    req_body: SetRegistryRequestBody,
    api_key: Annotated[ApiKey, Depends(get_api_key)],
):
    log.info("%s is setting Docker Registry to %s", api_key.log(), req_body.address)
    disco_host_home = await keyvalues.get_value(dbsession, "HOST_HOME")
    assert disco_host_home is not None
    await keyvalues.set_value(
        dbsession=dbsession, key="REGISTRY", value=req_body.address
    )
    return {
        "version": disco.__version__,
        "discoHost": await keyvalues.get_value(dbsession, "DISCO_HOST"),
        "registry": await keyvalues.get_value(dbsession, "REGISTRY"),
        # registryHost for backward compat, remove after 2027-02-01
        "registryHost": await keyvalues.get_value(dbsession, "REGISTRY"),
    }


@router.delete("/api/disco/registry")
async def registry_delete(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
):
    disco_host_home = await keyvalues.get_value(dbsession, "HOST_HOME")
    assert disco_host_home is not None
    registry = await keyvalues.get_value(dbsession, "REGISTRY")
    if registry is not None:
        node_ids = await docker.get_node_list()
        if len(node_ids) > 1:
            raise HTTPException(422, "Can't unset registry with many nodes running")
    log.info("%s is unsetting Docker Registry (was %s)", api_key.log(), registry)
    await keyvalues.set_value(dbsession=dbsession, key="REGISTRY", value=None)
    return {
        "version": disco.__version__,
        "discoHost": await keyvalues.get_value(dbsession, "DISCO_HOST"),
        "registry": await keyvalues.get_value(dbsession, "REGISTRY"),
        # registryHost for backward compat, remove after 2027-02-01
        "registryHost": await keyvalues.get_value(dbsession, "REGISTRY"),
    }
