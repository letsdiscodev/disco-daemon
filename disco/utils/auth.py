from secrets import token_hex

from sqlalchemy.orm.session import Session as DBSession

from disco.models import AuthenticationToken


def create_auth_token(dbsession: DBSession, name: str) -> AuthenticationToken:
    token = AuthenticationToken(
        id=token_hex(16),
        name=name,
    )
    dbsession.add(token)
    return token


def get_valid_token_by_id(
    dbsession: DBSession, token_id: str
) -> AuthenticationToken | None:
    # this function is here to eventually handle expired or deleted tokens
    token = get_auth_token_by_id(dbsession, token_id)
    if token is None:
        return None
    return token


def get_auth_token_by_id(
    dbsession: DBSession, token_id: str
) -> AuthenticationToken | None:
    return (
        dbsession.query(AuthenticationToken)
        .filter(AuthenticationToken.id == token_id)
        .first()
    )
