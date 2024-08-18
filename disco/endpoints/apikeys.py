import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key_sync
from disco.endpoints.dependencies import get_sync_db
from disco.models import ApiKey
from disco.utils.apikeys import (
    delete_api_key,
    get_all_api_keys,
    get_api_key_by_public_key_sync,
)
from disco.utils.encryption import obfuscate

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_sync)])


def get_api_key_from_url(
    dbsession: Annotated[DBSession, Depends(get_sync_db)],
    public_key: Annotated[str, Path()],
):
    api_key = get_api_key_by_public_key_sync(dbsession, public_key)
    if api_key is None:
        raise HTTPException(status_code=404)
    yield api_key


@router.get("/api/api-keys")
def api_keys_get(dbsession: Annotated[DBSession, Depends(get_sync_db)]):
    api_keys = get_all_api_keys(dbsession)
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
def api_key_delete(
    dbsession: Annotated[DBSession, Depends(get_sync_db)],
    api_key: Annotated[ApiKey, Depends(get_api_key_from_url)],
    by_api_key: Annotated[ApiKey, Depends(get_api_key_sync)],
):
    api_keys = get_all_api_keys(dbsession)
    if len(api_keys) == 1:
        assert api_key == api_keys[0]
        raise HTTPException(422, "Can't delete last API key.")
    delete_api_key(api_key=api_key, by_api_key=by_api_key)
    return {"deleted": True}
