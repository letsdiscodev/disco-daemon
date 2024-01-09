"""Script that runs when installing Disco on a server"""
import os
import sys

import transaction
from pyramid.paster import get_appsettings, setup_logging
from pyramid.scripts.common import parse_vars

from disco.models import get_engine, get_session_factory, get_tm_session
from disco.utils.auth import create_api_key


def usage(argv):
    cmd = os.path.basename(argv[0])
    print(
        "usage: %s <config_uri> <api_key_name>\n"
        '(example: "%s development.ini "Foo")' % (cmd, cmd)
    )
    sys.exit(1)


def main(argv=sys.argv):
    if len(argv) < 3:
        usage(argv)
    config_uri = argv[1]
    name = argv[2]
    options = parse_vars(argv[3:])
    setup_logging(config_uri)
    settings = get_appsettings(config_uri, options=options)
    engine = get_engine(settings)
    session_factory = get_session_factory(engine)
    with transaction.manager:
        dbsession = get_tm_session(session_factory, transaction.manager)
        api_key = create_api_key(dbsession=dbsession, name=name)
        print("Created API key:", api_key.id)
