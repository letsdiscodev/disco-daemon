"""Script that runs when installing Disco on a server"""
import logging
import sys

from disco.models.db import Session
from disco.utils.auth import create_api_key


def main(argv=sys.argv):
    logging.basicConfig(level=logging.INFO)
    name = argv[1]
    with Session() as dbsession:
        with dbsession.begin():
            api_key = create_api_key(dbsession=dbsession, name=name)
            print("Created API key:", api_key.id)
