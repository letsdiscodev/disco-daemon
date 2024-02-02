"""Script that runs when installing Disco on a server"""
import logging
import sys
import time

import requests

from disco.models.db import Session
from disco.utils import keyvalues
from disco.utils.caddy import add_disco_domain, set_empty_config

log = logging.getLogger(__name__)


def main(argv=sys.argv):
    domain = argv[1]
    logging.basicConfig(level=logging.INFO)
    print("Setting Caddy config for Disco domain")
    set_empty_config()
    add_disco_domain(domain)

    with Session() as dbsession:
        with dbsession.begin():
            keyvalues.set_value(dbsession=dbsession, key="DISCO_DOMAIN", value=domain)

    print(f"Waiting for TLS certificate of {domain}")
    for _ in range(120):
        try:
            requests.get(f"https://{domain}/")
            print("\nTLS certificate ready")
            return
        except requests.ConnectionError:
            print(".", end="")
            time.sleep(1)
    print(f"TLS certiciate of {domain} not working properly")
    exit(1)
