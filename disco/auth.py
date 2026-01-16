from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.endpoints.dependencies import get_db, get_db_sync
from disco.models import ApiKey
from disco.models.db import AsyncSession
from disco.utils import keyvalues
from disco.utils.apikeys import (
    get_api_key_by_public_key,
    get_api_key_by_public_key_sync,
    get_valid_api_key_by_id,
    get_valid_api_key_by_id_sync,
    record_api_key_usage,
    record_api_key_usage_sync,
)

basic_header = HTTPBasic(auto_error=False)
bearer_header = HTTPBearer(auto_error=False)


def get_api_key_sync(
    basic_credentials: Annotated[HTTPBasicCredentials | None, Depends(basic_header)],
    bearer_credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_header)
    ],
    dbsession: Annotated[DBSession, Depends(get_db_sync)],
):
    api_key_str = None
    if basic_credentials is not None:
        api_key_str = basic_credentials.username
    elif bearer_credentials is not None:
        bearer_jwt = bearer_credentials.credentials
        try:
            headers = jwt.get_unverified_header(bearer_jwt)
        except jwt.PyJWTError:
            headers = None
        if headers is not None:
            public_key = headers["kid"]
            api_key_for_public_key = get_api_key_by_public_key_sync(
                dbsession, public_key
            )
            if api_key_for_public_key is not None:
                disco_host = keyvalues.get_value_str_sync(dbsession, "DISCO_HOST")
                try:
                    jwt.decode(
                        bearer_jwt,
                        api_key_for_public_key.id,
                        algorithms=["HS256"],
                        audience=disco_host,
                        options=dict(
                            verify_signature=True,
                            verify_exp=True,
                        ),
                    )
                    api_key_str = api_key_for_public_key.id
                except jwt.PyJWTError:
                    pass
    if api_key_str is None:
        raise HTTPException(status_code=401)
    api_key = get_valid_api_key_by_id_sync(dbsession, api_key_str)
    if api_key is None:
        raise HTTPException(status_code=403)
    record_api_key_usage_sync(dbsession, api_key)
    yield api_key


async def get_api_key(
    basic_credentials: Annotated[HTTPBasicCredentials | None, Depends(basic_header)],
    bearer_credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_header)
    ],
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
):
    api_key_str = None
    if basic_credentials is not None:
        api_key_str = basic_credentials.username
    elif bearer_credentials is not None:
        bearer_jwt = bearer_credentials.credentials
        try:
            headers = jwt.get_unverified_header(bearer_jwt)
        except jwt.PyJWTError:
            headers = None
        if headers is not None:
            public_key = headers["kid"]
            api_key_for_public_key = await get_api_key_by_public_key(
                dbsession, public_key
            )
            if api_key_for_public_key is not None:
                disco_host = await keyvalues.get_value_str(dbsession, "DISCO_HOST")
                try:
                    jwt.decode(
                        bearer_jwt,
                        api_key_for_public_key.id,
                        algorithms=["HS256"],
                        audience=disco_host,
                        options=dict(
                            verify_signature=True,
                            verify_exp=True,
                        ),
                    )
                    api_key_str = api_key_for_public_key.id
                except jwt.PyJWTError:
                    pass
    if api_key_str is None:
        raise HTTPException(status_code=401)
    api_key = await get_valid_api_key_by_id(dbsession, api_key_str)
    if api_key is None:
        raise HTTPException(status_code=403)
    await record_api_key_usage(dbsession, api_key)
    yield api_key


async def get_api_key_wo_tx(
    basic_credentials: Annotated[HTTPBasicCredentials | None, Depends(basic_header)],
    bearer_credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_header)
    ],
):
    api_key_id = None
    async with AsyncSession.begin() as dbsession:
        api_key_str = None
        if basic_credentials is not None:
            api_key_str = basic_credentials.username
        elif bearer_credentials is not None:
            bearer_jwt = bearer_credentials.credentials
            try:
                headers = jwt.get_unverified_header(bearer_jwt)
            except jwt.PyJWTError:
                headers = None
            if headers is not None:
                public_key = headers["kid"]
                api_key_for_public_key = await get_api_key_by_public_key(
                    dbsession, public_key
                )
                if api_key_for_public_key is not None:
                    disco_host = await keyvalues.get_value_str(dbsession, "DISCO_HOST")
                    try:
                        jwt.decode(
                            bearer_jwt,
                            api_key_for_public_key.id,
                            algorithms=["HS256"],
                            audience=disco_host,
                            options=dict(
                                verify_signature=True,
                                verify_exp=True,
                            ),
                        )
                        api_key_str = api_key_for_public_key.id
                    except jwt.PyJWTError:
                        pass
        if api_key_str is None:
            raise HTTPException(status_code=401)
        api_key = await get_valid_api_key_by_id(dbsession, api_key_str)
        if api_key is None:
            raise HTTPException(status_code=403)
        api_key_id = api_key.id
        await record_api_key_usage(dbsession, api_key)

    yield api_key_id


async def validate_token(token: str) -> ApiKey | None:
    """
    Validate a token (either raw API key ID or JWT).
    Returns ApiKey if valid, None otherwise.
    """
    async with AsyncSession.begin() as dbsession:
        # First, try as raw API key ID (like Basic auth does)
        api_key = await get_valid_api_key_by_id(dbsession, token)
        if api_key is not None:
            await record_api_key_usage(dbsession, api_key)
            return api_key

        # Then try as JWT
        try:
            headers = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return None

        public_key = headers.get("kid")
        if not public_key:
            return None

        api_key_for_public_key = await get_api_key_by_public_key(dbsession, public_key)
        if api_key_for_public_key is None:
            return None

        disco_host = await keyvalues.get_value_str(dbsession, "DISCO_HOST")
        try:
            jwt.decode(
                token,
                api_key_for_public_key.id,
                algorithms=["HS256"],
                audience=disco_host,
                options=dict(
                    verify_signature=True,
                    verify_exp=True,
                ),
            )
        except jwt.PyJWTError:
            return None

        api_key = await get_valid_api_key_by_id(dbsession, api_key_for_public_key.id)
        if api_key is not None:
            await record_api_key_usage(dbsession, api_key)
        return api_key
