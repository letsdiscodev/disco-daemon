from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.endpoints.dependencies import get_db, get_sync_db
from disco.models.db import AsyncSession
from disco.utils.apikeys import (
    get_valid_api_key_by_id,
    get_valid_api_key_by_id_sync,
    record_api_key_usage,
    record_api_key_usage_sync,
)

security = HTTPBasic()


def get_api_key_sync(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    dbsession: Annotated[DBSession, Depends(get_sync_db)],
):
    api_key = get_valid_api_key_by_id_sync(dbsession, credentials.username)
    if api_key is None:
        raise HTTPException(status_code=403)
    record_api_key_usage_sync(dbsession, api_key)
    yield api_key


async def get_api_key(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
):
    api_key = await get_valid_api_key_by_id(dbsession, credentials.username)
    if api_key is None:
        raise HTTPException(status_code=403)
    await record_api_key_usage(dbsession, api_key)
    yield api_key


async def get_api_key_wo_tx(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
):
    api_key_id = None
    async with AsyncSession.begin() as dbsession:
        api_key = await get_valid_api_key_by_id(dbsession, credentials.username)
        if api_key is None:
            raise HTTPException(status_code=403)
        await record_api_key_usage(dbsession, api_key)
        api_key_id = api_key.id
    yield api_key_id
