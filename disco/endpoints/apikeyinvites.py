import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from sqlalchemy.orm.session import Session as DBSession

import disco
from disco.auth import get_api_key
from disco.endpoints.dependencies import get_db
from disco.models import ApiKey
from disco.utils import keyvalues
from disco.utils.apikeyinvites import (
    create_api_key_invite,
    get_api_key_invite_by_id,
    get_api_key_invite_by_name,
    invite_is_active,
    use_api_key_invite,
)
from disco.utils.apikeys import (
    get_api_key_by_name,
)

log = logging.getLogger(__name__)

router = APIRouter()


class NewApiKeyRequestBody(BaseModel):
    name: str = Field(..., max_length=255)


@router.post("/api-key-invites", status_code=201)
def api_keys_post(
    dbsession: Annotated[DBSession, Depends(get_db)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    req_body: NewApiKeyRequestBody,
):
    disco_host = keyvalues.get_value(dbsession, "DISCO_HOST")
    existing_api_key = get_api_key_by_name(dbsession, req_body.name)
    if existing_api_key is not None:
        raise RequestValidationError(
            errors=(
                ValidationError.from_exception_data(
                    "ValueError",
                    [
                        InitErrorDetails(
                            type=PydanticCustomError(
                                "value_error", "API Key name already exists"
                            ),
                            loc=("body", "name"),
                            input=req_body.name,
                        )
                    ],
                )
            ).errors()
        )

    existing_invite = get_api_key_invite_by_name(dbsession, req_body.name)
    if existing_invite is not None:
        raise RequestValidationError(
            errors=(
                ValidationError.from_exception_data(
                    "ValueError",
                    [
                        InitErrorDetails(
                            type=PydanticCustomError(
                                "value_error",
                                "API Key name already used in other invite",
                            ),
                            loc=("body", "name"),
                            input=req_body.name,
                        )
                    ],
                )
            ).errors()
        )
    invite = create_api_key_invite(
        dbsession=dbsession,
        name=req_body.name,
        by_api_key=api_key,
    )
    return {
        "apiKeyInvite": {
            "url": f"https://{disco_host}/.disco/api-key-invites/{invite.id}",
            "expires": invite.expires.isoformat(),
        },
    }


def get_api_key_invite_from_url(
    invite_id: Annotated[str, Path()],
    dbsession: Annotated[DBSession, Depends(get_db)],
):
    invite = get_api_key_invite_by_id(dbsession, invite_id)
    if invite is None:
        raise HTTPException(status_code=404)
    yield invite


RESP_TXT = (
    "To accept invite, install Disco CLI (https://letsdisco.dev) "
    "and run this command:\n\n    "
    "disco invite:accept https://{disco_host}/.disco/api-key-invites/{invite_id}"
)


@router.get("/api-key-invites/{invite_id}", response_class=PlainTextResponse)
def api_key_invite_get(
    dbsession: Annotated[DBSession, Depends(get_db)],
    invite: Annotated[ApiKey, Depends(get_api_key_invite_from_url)],
):
    disco_host = keyvalues.get_value(dbsession, "DISCO_HOST")
    return RESP_TXT.format(disco_host=disco_host, invite_id=invite.id)


@router.post("/api-key-invites/{invite_id}")
def api_key_invite_post(
    dbsession: Annotated[DBSession, Depends(get_db)],
    invite: Annotated[ApiKey, Depends(get_api_key_invite_from_url)],
):
    if not invite_is_active(invite):
        raise HTTPException(422, "Invite expired")
    api_key = use_api_key_invite(dbsession, invite)
    return {
        "apiKey": {
            "name": api_key.name,
            "privateKey": api_key.id,
            "publicKey": api_key.public_key,
        },
        "meta": {
            "version": disco.__version__,
            "discoHost": keyvalues.get_value(dbsession, "DISCO_HOST"),
            "registryHost": keyvalues.get_value(dbsession, "REGISTRY_HOST"),
        },
    }
