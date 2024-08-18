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

from disco.endpoints.dependencies import get_db, get_sync_db
from disco.models.db import AsyncSession
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
    dbsession: Annotated[DBSession, Depends(get_sync_db)],
):
    api_key_str = None
    if basic_credentials is not None:
        api_key_str = basic_credentials.username
    elif bearer_credentials is not None:
        bearer_jwt = bearer_credentials.credentials
        try:
            unverified_jwt = jwt.decode(bearer_jwt, options={"verify_signature": False})
        except jwt.PyJWTError:
            unverified_jwt = None
        if unverified_jwt is not None:
            public_key = unverified_jwt["kid"]
            api_key_for_public_key = get_api_key_by_public_key_sync(
                dbsession, public_key
            )
            if api_key_for_public_key is not None:
                try:
                    jwt.decode(
                        bearer_jwt, api_key_for_public_key.id, algorithms=["HS256"]
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
            unverified_jwt = jwt.decode(bearer_jwt, options={"verify_signature": False})
        except jwt.PyJWTError:
            unverified_jwt = None
        if unverified_jwt is not None:
            public_key = unverified_jwt["kid"]
            api_key_for_public_key = await get_api_key_by_public_key(
                dbsession, public_key
            )
            if api_key_for_public_key is not None:
                try:
                    jwt.decode(
                        bearer_jwt, api_key_for_public_key.id, algorithms=["HS256"]
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
                unverified_jwt = jwt.decode(
                    bearer_jwt, options={"verify_signature": False}
                )
            except jwt.PyJWTError:
                unverified_jwt = None
            if unverified_jwt is not None:
                public_key = unverified_jwt["kid"]
                api_key_for_public_key = await get_api_key_by_public_key(
                    dbsession, public_key
                )
                if api_key_for_public_key is not None:
                    try:
                        jwt.decode(
                            bearer_jwt, api_key_for_public_key.id, algorithms=["HS256"]
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

    yield api_key_id
