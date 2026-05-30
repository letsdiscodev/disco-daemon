import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

import disco
from disco.auth import get_api_key_wo_tx
from disco.models.db import AsyncSession
from disco.utils import keyvalues
from disco.utils.apikeyinvites import (
    create_api_key_invite,
    get_api_key_invite_by_id,
    invite_is_active,
    use_api_key_invite,
)
from disco.utils.apikeys import get_api_key_by_id

log = logging.getLogger(__name__)

router = APIRouter()


class NewApiKeyRequestBody(BaseModel):
    name: str = Field(..., max_length=255)


@router.post("/api/api-key-invites", status_code=201)
async def api_keys_post(
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    req_body: NewApiKeyRequestBody,
):
    async with AsyncSession.begin() as dbsession:
        api_key = await get_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
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


async def get_api_key_invite_id_from_url(
    invite_id: Annotated[str, Path()],
):
    async with AsyncSession.begin() as dbsession:
        invite = await get_api_key_invite_by_id(dbsession, invite_id)
        if invite is None:
            raise HTTPException(status_code=404)
    yield invite_id


RESP_TXT = (
    "To accept invite, install Disco CLI (https://letsdisco.dev) "
    "and run this command:\n\n    "
    "disco invite:accept https://{disco_host}/api-key-invites/{invite_id}"
)


@router.get("/api-key-invites/{invite_id}", response_class=PlainTextResponse)
async def api_key_invite_get(
    invite_id: Annotated[str, Depends(get_api_key_invite_id_from_url)],
):
    async with AsyncSession.begin() as dbsession:
        invite = await get_api_key_invite_by_id(dbsession, invite_id)
        assert invite is not None
        disco_host = await keyvalues.get_value(dbsession, "DISCO_HOST")
        return RESP_TXT.format(disco_host=disco_host, invite_id=invite.id)


@router.post("/api-key-invites/{invite_id}")
async def api_key_invite_post(
    invite_id: Annotated[str, Depends(get_api_key_invite_id_from_url)],
):
    async with AsyncSession.begin() as dbsession:
        invite = await get_api_key_invite_by_id(dbsession, invite_id)
        assert invite is not None
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
