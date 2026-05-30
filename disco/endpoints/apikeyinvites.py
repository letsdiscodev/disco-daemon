import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

import disco
from disco.auth import get_api_key
from disco.endpoints.dependencies import get_db
from disco.models import ApiKey, ApiKeyInvite
from disco.utils import keyvalues
from disco.utils.apikeyinvites import (
    create_api_key_invite,
    get_api_key_invite_by_id,
    invite_is_active,
    use_api_key_invite,
)

log = logging.getLogger(__name__)

router = APIRouter()


class NewApiKeyRequestBody(BaseModel):
    name: str = Field(..., max_length=255)


@router.post("/api/api-key-invites", status_code=201)
async def api_keys_post(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    req_body: NewApiKeyRequestBody,
):
    disco_host = await keyvalues.get_value(dbsession, "DISCO_HOST")
    invite = await create_api_key_invite(
        dbsession=dbsession,
        name=req_body.name,
        by_api_key=api_key,
    )
    return {
        "apiKeyInvite": {
            "url": f"https://{disco_host}/api-key-invites/{invite.id}",
            "expires": invite.expires.isoformat(),
        },
    }


async def get_api_key_invite_from_url(
    invite_id: Annotated[str, Path()],
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
):
    invite = await get_api_key_invite_by_id(dbsession, invite_id)
    if invite is None:
        raise HTTPException(status_code=404)
    yield invite


RESP_TXT = (
    "To accept invite, install Disco CLI (https://letsdisco.dev) "
    "and run this command:\n\n    "
    "disco invite:accept https://{disco_host}/api-key-invites/{invite_id}"
)


@router.get("/api-key-invites/{invite_id}", response_class=PlainTextResponse)
async def api_key_invite_get(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    invite: Annotated[ApiKeyInvite, Depends(get_api_key_invite_from_url)],
):
    disco_host = await keyvalues.get_value(dbsession, "DISCO_HOST")
    return RESP_TXT.format(disco_host=disco_host, invite_id=invite.id)


@router.post("/api-key-invites/{invite_id}")
async def api_key_invite_post(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    invite: Annotated[ApiKeyInvite, Depends(get_api_key_invite_from_url)],
):
    if not invite_is_active(invite):
        raise HTTPException(422, "Invite expired")
    api_key = await use_api_key_invite(dbsession, invite)
    return {
        "apiKey": {
            "name": api_key.name,
            "privateKey": api_key.id,
            "publicKey": api_key.public_key,
        },
        "meta": {
            "version": disco.__version__,
            "discoHost": await keyvalues.get_value(dbsession, "DISCO_HOST"),
            "registry": await keyvalues.get_value(dbsession, "REGISTRY"),
            # registryHost for backward compat, remove after 2027-02-01
            "registryHost": await keyvalues.get_value(dbsession, "REGISTRY"),
        },
    }
