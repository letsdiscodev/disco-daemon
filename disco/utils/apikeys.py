import logging
from datetime import datetime, timezone
from secrets import token_hex

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as DBSession
from sqlalchemy.orm import selectinload

from disco.models import ApiKey, ApiKeyUsage
from disco.utils import events

log = logging.getLogger(__name__)


async def create_api_key(dbsession: DBSession, name: str) -> ApiKey:
    api_key = ApiKey(
        id=token_hex(16),
        name=name,
        public_key=token_hex(16),
    )
    dbsession.add(api_key)
    log.info("Created API key %s", api_key.log())
    events.api_key_created(public_key=api_key.public_key, name=name)
    return api_key


async def get_valid_api_key_by_id(
    dbsession: DBSession, api_key_id: str
) -> ApiKey | None:
    api_key = await get_api_key_by_id(dbsession, api_key_id)
    if api_key is None:
        return None
    if api_key.deleted is not None:
        return None
    return api_key


async def get_all_api_keys(dbsession: DBSession) -> list[ApiKey]:
    stmt = (
        select(ApiKey)
        .where(ApiKey.deleted.is_(None))
        .order_by(ApiKey.created.asc())
        .options(selectinload(ApiKey.usages))
    )
    result = await dbsession.execute(stmt)
    return list(result.scalars().all())


async def get_api_key_by_id(dbsession: DBSession, api_key_id: str) -> ApiKey | None:
    return await dbsession.get(ApiKey, api_key_id)


async def get_api_key_by_public_key(
    dbsession: DBSession, public_key: str
) -> ApiKey | None:
    stmt = (
        select(ApiKey)
        .where(ApiKey.public_key == public_key)
        .where(ApiKey.deleted.is_(None))
    )
    result = await dbsession.execute(stmt)
    return result.scalars().first()


def delete_api_key(api_key: ApiKey, by_api_key: ApiKey) -> None:
    assert api_key.deleted is None
    log.info("Marking API key as deleted %s by %s", api_key.log(), by_api_key.log())
    api_key.deleted = datetime.now(timezone.utc)
    events.api_key_removed(public_key=api_key.public_key, name=api_key.name)


async def record_api_key_usage(api_key_id: str) -> None:
    from disco.models.db import Session
    from disco.utils.worker import worker

    created = datetime.now(timezone.utc)

    async def write_usage() -> None:
        async with Session.begin() as dbsession:
            dbsession.add(ApiKeyUsage(api_key_id=api_key_id, created=created))

    await worker.enqueue(write_usage)
