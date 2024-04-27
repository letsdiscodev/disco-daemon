import logging
from datetime import datetime, timezone
from secrets import token_hex

from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey

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


def get_valid_api_key_by_id(dbsession: DBSession, api_key_id: str) -> ApiKey | None:
    api_key = get_api_key_by_id(dbsession, api_key_id)
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


def get_api_key_by_id(dbsession: DBSession, api_key_id: str) -> ApiKey | None:
    return dbsession.query(ApiKey).filter(ApiKey.id == api_key_id).first()


def get_api_key_by_public_key(dbsession: DBSession, public_key: str) -> ApiKey | None:
    return (
        dbsession.query(ApiKey)
        .filter(ApiKey.public_key == public_key)
        .filter(ApiKey.deleted.is_(None))
        .first()
    )


def get_api_key_by_name(dbsession: DBSession, name: str) -> ApiKey | None:
    return (
        dbsession.query(ApiKey)
        .filter(ApiKey.name == name)
        .filter(ApiKey.deleted.is_(None))
        .first()
    )


def delete_api_key(api_key: ApiKey, by_api_key: ApiKey) -> None:
    assert api_key.deleted is None
    log.info("Marking API key as deleted %s by %s", api_key.log(), by_api_key.log())
    api_key.deleted = datetime.now(timezone.utc)
