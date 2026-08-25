import asyncio
import logging
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from disco.models import ApiKey
from disco.models.db import AsyncReadSession
from disco.utils import keyvalues
from disco.utils.apikeys import (
    get_api_key_by_public_key,
    get_valid_api_key_by_id,
    record_api_key_usage,
)
from disco.utils.backup_auth import (
    append_emergency_auth_audit,
    find_api_key_by_id_in_backup,
)

log = logging.getLogger(__name__)

EMERGENCY_DB_TIMEOUT_S = 3.0

basic_header = HTTPBasic(auto_error=False)
bearer_header = HTTPBearer(auto_error=False)


async def get_api_key_wo_tx(
    basic_credentials: Annotated[HTTPBasicCredentials | None, Depends(basic_header)],
    bearer_credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_header)
    ],
):
    api_key_id = None
    async with AsyncReadSession.begin() as dbsession:
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

    await record_api_key_usage(api_key_id)
    yield api_key_id


async def get_api_key_emergency_capable(
    basic_credentials: Annotated[HTTPBasicCredentials | None, Depends(basic_header)],
    bearer_credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_header)
    ],
):
    """Falls back to latest.db when the DB is unreachable (Basic auth only)."""
    # The timeout must not span the yield: emergency endpoints run long Docker work.
    resolved_api_key_id: str | None = None
    db_unreachable = False
    try:
        async with asyncio.timeout(EMERGENCY_DB_TIMEOUT_S):
            async with AsyncReadSession.begin() as dbsession:
                api_key_str = await _resolve_credentials_via_db(
                    basic_credentials, bearer_credentials, dbsession
                )
                if api_key_str is None:
                    raise HTTPException(status_code=401)
                api_key = await get_valid_api_key_by_id(dbsession, api_key_str)
                if api_key is None:
                    raise HTTPException(status_code=403)
                resolved_api_key_id = api_key.id
    except HTTPException:
        raise
    except (asyncio.TimeoutError, OperationalError, OSError) as exc:
        log.warning(
            "Emergency auth: DB unreachable (%s); attempting backup fallback", exc
        )
        db_unreachable = True

    if resolved_api_key_id is not None:
        await record_api_key_usage(resolved_api_key_id)
        yield resolved_api_key_id
        return

    if not db_unreachable:
        raise HTTPException(status_code=401)
    if basic_credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Basic auth required while the dqlite cluster is unavailable",
        )
    api_key_id = basic_credentials.username
    if not api_key_id:
        raise HTTPException(status_code=401)
    cached = find_api_key_by_id_in_backup(api_key_id)
    if cached is None:
        raise HTTPException(status_code=403)
    log.info("Emergency auth granted to API key %s (via backup)", cached.public_key)
    append_emergency_auth_audit(
        api_key_id=cached.id,
        public_key=cached.public_key,
        reason="db-unreachable",
    )
    yield cached.id


async def _resolve_credentials_via_db(
    basic_credentials: HTTPBasicCredentials | None,
    bearer_credentials: HTTPAuthorizationCredentials | None,
    dbsession: AsyncDBSession,
) -> str | None:
    if basic_credentials is not None:
        return basic_credentials.username
    if bearer_credentials is None:
        return None
    bearer_jwt = bearer_credentials.credentials
    try:
        headers = jwt.get_unverified_header(bearer_jwt)
    except jwt.PyJWTError:
        return None
    public_key = headers.get("kid")
    if public_key is None:
        return None
    api_key_for_public_key = await get_api_key_by_public_key(dbsession, public_key)
    if api_key_for_public_key is None:
        return None
    disco_host = await keyvalues.get_value_str(dbsession, "DISCO_HOST")
    try:
        jwt.decode(
            bearer_jwt,
            api_key_for_public_key.id,
            algorithms=["HS256"],
            audience=disco_host,
            options=dict(verify_signature=True, verify_exp=True),
        )
    except jwt.PyJWTError:
        return None
    return api_key_for_public_key.id


async def validate_token(token: str) -> ApiKey | None:
    """
    Validate a token (either raw API key ID or JWT).
    Returns ApiKey if valid, None otherwise.
    """
    async with AsyncReadSession.begin() as dbsession:
        # First, try as raw API key ID (like Basic auth does)
        api_key = await get_valid_api_key_by_id(dbsession, token)
        if api_key is not None:
            await record_api_key_usage(api_key.id)
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
            await record_api_key_usage(api_key.id)
        return api_key
