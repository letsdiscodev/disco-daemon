import socket

from sqlalchemy.orm.session import Session as DBSession

from disco.utils import keyvalues


def domain_points_to_here(dbsession: DBSession, domain: str) -> bool:
    disco_ip = keyvalues.get_value(dbsession=dbsession, key="DISCO_IP")
    try:
        domain_ip = socket.gethostbyname(domain)
    except socket.gaierror:
        return False
    return domain_ip == disco_ip
