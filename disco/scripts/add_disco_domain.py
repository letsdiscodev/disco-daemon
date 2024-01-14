"""Script that runs when installing Disco on a server"""
import os
import sys
import time

import requests
import transaction
from pyramid.paster import get_appsettings, setup_logging
from pyramid.scripts.common import parse_vars

from disco.models import get_engine, get_session_factory, get_tm_session
from disco.utils import keyvalues
from disco.utils.caddy import add_disco_domain, set_empty_config


def usage(argv):
    cmd = os.path.basename(argv[0])
    print(
        "usage: %s <config_uri> <disco_domain>\n"
        '(example: "%s development.ini disco.example.com")' % (cmd, cmd)
    )
    sys.exit(1)


def main(argv=sys.argv):
    if len(argv) < 3:
        usage(argv)
    config_uri = argv[1]
    domain = argv[2]
    options = parse_vars(argv[3:])
    setup_logging(config_uri)
    settings = get_appsettings(config_uri, options=options)
    engine = get_engine(settings)
    session_factory = get_session_factory(engine)
    print("Setting Caddy config for Disco domain")
    set_empty_config()
    add_disco_domain(domain)
    with transaction.manager:
        dbsession = get_tm_session(session_factory, transaction.manager)

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
