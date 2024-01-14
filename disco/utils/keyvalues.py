from disco.models import KeyValue


def get_value(dbsession, key):
    key_value = dbsession.query(KeyValue).get(key)
    if key_value is None:
        return None
    return key_value.value


def set_value(dbsession, key, value):
    key_value = dbsession.query(KeyValue).get(key)
    if key_value is not None:
        key_value.value = value
    else:
        key_value = KeyValue(
            key=key,
            value=value,
        )
        dbsession.add(key_value)


def delete_value(dbsession, key):
    key = dbsession.query(KeyValue).get(key)
    if key is not None:
        dbsession.delete(key)
