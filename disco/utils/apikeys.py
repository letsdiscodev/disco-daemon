import logging
from datetime import datetime, timezone
from secrets import token_hex

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, ApiKeyUsage
from disco.utils import events

log = logging.getLogger(__name__)


def create_api_key(dbsession: DBSession, name: str) -> ApiKey:
    api_key = ApiKey(
        id=token_hex(16),
        name=name,
        public_key=token_hex(16),
    )
    dbsession.add(api_key)
    log.info("Created API key %s", api_key.log())
    events.api_key_created(public_key=api_key.public_key, name=name)
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
    events.api_key_removed(public_key=api_key.public_key, name=api_key.name)


def record_api_key_usage_sync(dbsession: DBSession, api_key: ApiKey) -> None:
    # Buffer in memory; a periodic flusher writes them in one batched
    # transaction. Doing the INSERT inline made every authenticated
    # request a writer, so N concurrent requests fought dqlite's
    # single-writer lock and timed out under load (this is the
    # observed regression vs SQLite, where the same writes were free).
    _buffer_usage(api_key.id)


async def record_api_key_usage(dbsession: AsyncDBSession, api_key: ApiKey) -> None:
    _buffer_usage(api_key.id)


# ---- Buffered usage writer ------------------------------------------------

import asyncio  # noqa: E402

_pending_usages: list[tuple[str, datetime]] = []
_pending_usages_lock = asyncio.Lock()
_FLUSH_INTERVAL_SECONDS = 5.0


def _buffer_usage(api_key_id: str) -> None:
    _pending_usages.append((api_key_id, datetime.now(timezone.utc)))


async def flush_api_key_usages_forever() -> None:
    """Background task: periodically flush buffered ApiKeyUsage rows.

    One batched INSERT per interval. Avoids per-request dqlite writes
    that would otherwise serialise the entire request stream on the
    single-writer lock.
    """
    from disco.models.db import AsyncSession

    while True:
        try:
            await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
            async with _pending_usages_lock:
                batch = _pending_usages[:]
                _pending_usages.clear()
            if not batch:
                continue
            async with AsyncSession.begin() as dbsession:
                dbsession.add_all(
                    ApiKeyUsage(created=ts, api_key_id=key_id)
                    for key_id, ts in batch
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Failed to flush api_key_usages batch")
