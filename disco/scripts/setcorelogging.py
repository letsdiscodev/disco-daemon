"""Set core syslogs."""

import asyncio
import logging
import sys

from disco.models.db import Session
from disco.utils import docker, keyvalues
from disco.utils.syslog import logspout_url, set_core_syslogs

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    urls = sys.argv[1:]
    with Session.begin() as dbsession:
        disco_host = keyvalues.get_value_sync(dbsession, "DISCO_HOST")
        assert disco_host is not None
        syslog_urls = set_core_syslogs(dbsession, urls)
    asyncio.run(
        docker.set_syslog_service(
            disco_host, [logspout_url(syslog_url) for syslog_url in syslog_urls]
        )
    )
