import logging
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, CorsOrigin

log = logging.getLogger(__name__)


async def allow_origin(
    dbsession: AsyncDBSession, origin: str, by_api_key: ApiKey
) -> None:
    from disco.middleware import update_cors

    cors_origin = await get_cors_origin(dbsession, origin)
    if cors_origin is None:
        cors_origin = CorsOrigin(
            origin=origin,
            by_api_key=by_api_key,
        )
        dbsession.add(cors_origin)
        all_origins = await get_all_cors_origins(dbsession)
        update_cors([o.origin for o in all_origins])
        log.info(
            "Added CORS origin to allowed origins in database %s", cors_origin.log()
        )
    else:
        log.info(
            "CORS origin already present in database, not adding %s", cors_origin.log()
        )


async def get_cors_origin(dbsession: AsyncDBSession, origin: str) -> CorsOrigin | None:
    stmt = select(CorsOrigin).where(CorsOrigin.origin == origin)
    result = await dbsession.execute(stmt)
    return result.scalars().first()


async def get_all_cors_origins(dbsession: AsyncDBSession) -> Sequence[CorsOrigin]:
    stmt = select(CorsOrigin)
    result = await dbsession.execute(stmt)
    return result.scalars().all()


def get_all_cors_origins_sync(dbsession: DBSession) -> Sequence[CorsOrigin]:
    stmt = select(CorsOrigin)
    result = dbsession.execute(stmt)
    return result.scalars().all()
