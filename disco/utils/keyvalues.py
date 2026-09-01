from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from disco.models import KeyValue


class KeyNotFoundError(Exception):
    pass


async def get_value_str(dbsession: AsyncDBSession, key: str) -> str:
    key_value = await dbsession.get(KeyValue, key)
    if key_value is None:
        raise KeyNotFoundError(f"Key {key} not found")
    if key_value.value is None:
        raise KeyNotFoundError(f"Key {key} has value None")
    return key_value.value


async def get_value(dbsession: AsyncDBSession, key: str) -> str | None:
    key_value = await dbsession.get(KeyValue, key)
    if key_value is None:
        return None
    return key_value.value


async def set_value(dbsession: AsyncDBSession, key: str, value: str | None) -> None:
    key_value = await dbsession.get(KeyValue, key)
    if key_value is not None:
        key_value.value = value
    else:
        key_value = KeyValue(
            key=key,
            value=value,
        )
        dbsession.add(key_value)


async def delete_value(dbsession: AsyncDBSession, key: str) -> None:
    key_value = await dbsession.get(KeyValue, key)
    if key_value is not None:
        await dbsession.delete(key_value)


async def all_key_values_with_prefix(
    dbsession: AsyncDBSession, prefix: str
) -> list[tuple[str, str | None]]:
    stmt = select(KeyValue).where(KeyValue.key.startswith(prefix))
    result = await dbsession.execute(stmt)
    return [(kv.key, kv.value) for kv in result.scalars()]
