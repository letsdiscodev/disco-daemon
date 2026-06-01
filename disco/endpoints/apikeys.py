import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field

from disco.auth import get_api_key_wo_tx
from disco.models.db import AsyncReadSession, AsyncSession
from disco.utils.apikeys import (
    delete_api_key,
    get_all_api_keys,
    get_api_key_by_id,
    get_api_key_by_public_key,
)
from disco.utils.encryption import obfuscate

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])


async def get_api_key_public_key_from_url(
    public_key: Annotated[str, Path()],
):
    async with AsyncReadSession.begin() as dbsession:
        api_key = await get_api_key_by_public_key(dbsession, public_key)
        if api_key is None:
            raise HTTPException(status_code=404)
    yield public_key


@router.get("/api/api-keys")
async def api_keys_get():
    async with AsyncReadSession.begin() as dbsession:
        api_keys = await get_all_api_keys(dbsession)
        return {
            "apiKeys": [
                {
                    "name": api_key.name,
                    "publicKey": api_key.public_key,
                    "privateKey": obfuscate(api_key.id),
                    "lastUsed": api_key.usages[0].created.isoformat()
                    if len(api_key.usages) > 0
                    else None,
                }
                for api_key in api_keys
            ],
        }


class NewApiKeyRequestBody(BaseModel):
    name: str = Field(..., max_length=255)


@router.delete("/api/api-keys/{public_key}", status_code=200)
async def api_key_delete(
    public_key: Annotated[str, Depends(get_api_key_public_key_from_url)],
    by_api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
):
    async with AsyncSession.begin() as dbsession:
        api_key = await get_api_key_by_public_key(dbsession, public_key)
        assert api_key is not None
        by_api_key = await get_api_key_by_id(dbsession, by_api_key_id)
        assert by_api_key is not None
        api_keys = await get_all_api_keys(dbsession)
        if len(api_keys) == 1:
            assert api_key == api_keys[0]
            raise HTTPException(422, "Can't delete last API key.")
        delete_api_key(api_key=api_key, by_api_key=by_api_key)
    return {"deleted": True}
