"""Script that runs when installing Disco on a server"""
import logging

from disco.models.db import engine
from disco.models.meta import metadata


def main():
    logging.basicConfig(level=logging.INFO)
    metadata.create_all(engine)
