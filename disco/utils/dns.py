import asyncio
import socket

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.utils import keyvalues


def domain_points_to_here_sync(dbsession: DBSession, domain: str) -> bool:
    disco_host = keyvalues.get_value_sync(dbsession=dbsession, key="DISCO_HOST")
    assert disco_host is not None
    try:
        domain_ip = socket.gethostbyname(domain)
        disco_ip = socket.gethostbyname(disco_host)
    except socket.gaierror:
        return False
    return domain_ip == disco_ip


async def domain_points_to_here(dbsession: AsyncDBSession, domain: str) -> bool:
    disco_host = await keyvalues.get_value(dbsession=dbsession, key="DISCO_HOST")
    assert disco_host is not None

    def point_to_same_ip() -> bool:
        # TODO socket.gethostbyname async?
        try:
            domain_ip = socket.gethostbyname(domain)
            disco_ip = socket.gethostbyname(disco_host)
        except socket.gaierror:
            return False
        return domain_ip == disco_ip

    return await asyncio.get_event_loop().run_in_executor(None, point_to_same_ip)
