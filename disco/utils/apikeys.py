import logging
from datetime import datetime, timezone
from secrets import token_hex

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, ApiKeyUsage

log = logging.getLogger(__name__)


def create_api_key(dbsession: DBSession, name: str) -> ApiKey:
    api_key = ApiKey(
        id=token_hex(16),
        name=name,
        public_key=token_hex(16),
    )
    dbsession.add(api_key)
    log.info("Created API key %s", api_key.log())
    return api_key


def get_valid_api_key_by_id_sync(
    dbsession: DBSession, api_key_id: str
) -> ApiKey | None:
    api_key = get_api_key_by_id_sync(dbsession, api_key_id)
    if api_key is None:
        return None
    if api_key.deleted is not None:
        return None
    return api_key


async def get_valid_api_key_by_id(
    dbsession: AsyncDBSession, api_key_id: str
) -> ApiKey | None:
    api_key = await get_api_key_by_id(dbsession, api_key_id)
    if api_key is None:
        return None
    if api_key.deleted is not None:
        return None
    return api_key


def get_all_api_keys(dbsession: DBSession) -> list[ApiKey]:
    return (
        dbsession.query(ApiKey)
        .filter(ApiKey.deleted.is_(None))
        .order_by(ApiKey.created.asc())
        .all()
    )


def get_api_key_by_id_sync(dbsession: DBSession, api_key_id: str) -> ApiKey | None:
    return dbsession.query(ApiKey).filter(ApiKey.id == api_key_id).first()


async def get_api_key_by_id(
    dbsession: AsyncDBSession, api_key_id: str
) -> ApiKey | None:
    return await dbsession.get(ApiKey, api_key_id)


def get_api_key_by_public_key_sync(
    dbsession: DBSession, public_key: str
) -> ApiKey | None:
    stmt = (
        select(ApiKey)
        .where(ApiKey.public_key == public_key)
        .where(ApiKey.deleted.is_(None))
    )
    result = dbsession.execute(stmt)
    return result.scalars().first()


async def get_api_key_by_public_key(
    dbsession: AsyncDBSession, public_key: str
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


def record_api_key_usage_sync(dbsession: DBSession, api_key: ApiKey) -> None:
    dbsession.add(ApiKeyUsage(created=datetime.now(timezone.utc), api_key=api_key))


async def record_api_key_usage(dbsession: AsyncDBSession, api_key: ApiKey) -> None:
    dbsession.add(ApiKeyUsage(created=datetime.now(timezone.utc), api_key=api_key))
