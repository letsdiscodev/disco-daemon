from secrets import token_hex

from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey


def create_api_key(dbsession: DBSession, name: str) -> ApiKey:
    api_key = ApiKey(
        id=token_hex(16),
        name=name,
    )
    dbsession.add(api_key)
    return api_key


def get_valid_api_key_by_id(dbsession: DBSession, api_key_id: str) -> ApiKey | None:
    # this function is here to eventually handle expired or deleted API keys
    api_key = get_api_key_by_id(dbsession, api_key_id)
    if api_key is None:
        return None
    return api_key


def get_api_key_by_id(dbsession: DBSession, api_key_id: str) -> ApiKey | None:
    return dbsession.query(ApiKey).filter(ApiKey.id == api_key_id).first()
