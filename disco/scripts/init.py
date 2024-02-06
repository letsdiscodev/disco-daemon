"""Script that runs when installing Disco on a server"""
import logging
import sys

from disco.models.db import Session, engine
from disco.models.meta import metadata
from disco.utils import caddy, keyvalues


def main(argv=sys.argv):
    logging.basicConfig(level=logging.INFO)
    disco_ip = argv[1]
    metadata.create_all(engine)
    with Session() as dbsession:
        with dbsession.begin():
            keyvalues.set_value(dbsession=dbsession, key="DISCO_IP", value=disco_ip)
            keyvalues.set_value(dbsession=dbsession, key="DISCO_HOST", value=disco_ip)
            keyvalues.set_value(
                dbsession=dbsession, key="REGISTRY_HOST", value=disco_ip
            )

    caddy.init_config(disco_ip)
