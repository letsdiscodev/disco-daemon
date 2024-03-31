import logging
from datetime import datetime, timedelta
from secrets import token_hex

from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, ApiKeyInvite
from disco.utils.apikeys import create_api_key

log = logging.getLogger(__name__)


def create_api_key_invite(
    dbsession: DBSession, name: str, by_api_key: ApiKey
) -> ApiKeyInvite:
    invite = ApiKeyInvite(
        id=token_hex(16),
        name=name,
        expires=datetime.utcnow() + timedelta(days=1),
        by_api_key=by_api_key,
    )
    dbsession.add(invite)
    log.info("Created API Key invite %s by %s", invite.log(), by_api_key.log())
    return invite


def get_api_key_invite_by_id(
    dbsession: DBSession, invite_id: str
) -> ApiKeyInvite | None:
    return dbsession.query(ApiKeyInvite).filter(ApiKeyInvite.id == invite_id).first()


def invite_is_active(invite):
    return invite.expires > datetime.utcnow() and invite.api_key_id is None


def get_api_key_invite_by_name(dbsession: DBSession, name: str) -> ApiKeyInvite | None:
    return (
        dbsession.query(ApiKeyInvite)
        .filter(ApiKeyInvite.name == name)
        .filter(ApiKeyInvite.expires > datetime.utcnow())
        .filter(ApiKeyInvite.api_key_id.is_(None))
        .first()
    )


def use_api_key_invite(dbsession: DBSession, invite: ApiKeyInvite) -> ApiKey:
    assert invite.expires > datetime.utcnow()
    assert invite.api_key_id is None
    api_key = create_api_key(dbsession, invite.name)
    invite.api_key = api_key
    return api_key
