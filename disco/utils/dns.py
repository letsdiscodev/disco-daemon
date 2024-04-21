import socket

from sqlalchemy.orm.session import Session as DBSession

from disco.utils import keyvalues


def domain_points_to_here(dbsession: DBSession, domain: str) -> bool:
    disco_host = keyvalues.get_value(dbsession=dbsession, key="DISCO_HOST")
    assert disco_host is not None
    try:
        domain_ip = socket.gethostbyname(domain)
        disco_ip = socket.gethostbyname(disco_host)
    except socket.gaierror:
        return False
    return domain_ip == disco_ip
