from sqlalchemy.orm.session import Session as DBSession

from disco.models import KeyValue


def get_value(dbsession: DBSession, key: str) -> str | None:
    key_value = dbsession.query(KeyValue).get(key)
    if key_value is None:
        return None
    return key_value.value


def set_value(dbsession: DBSession, key: str, value: str | None) -> None:
    key_value = dbsession.query(KeyValue).get(key)
    if key_value is not None:
        key_value.value = value
    else:
        key_value = KeyValue(
            key=key,
            value=value,
        )
        dbsession.add(key_value)


def delete_value(dbsession: DBSession, key: str) -> None:
    key = dbsession.query(KeyValue).get(key)
    if key is not None:
        dbsession.delete(key)
