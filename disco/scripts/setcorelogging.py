"""Set core syslogs."""

import asyncio
import logging
import sys

from disco.models.db import Session, build_engines
from disco.utils import keyvalues
from disco.utils.syslog import set_core_syslogs, set_syslog_services

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    urls = sys.argv[1:]
    asyncio.run(_main(urls))


async def _main(urls: list[str]) -> None:
    await build_engines()
    async with Session.begin() as dbsession:
        disco_host = await keyvalues.get_value_str(dbsession, "DISCO_HOST")
        syslog_urls = await set_core_syslogs(dbsession, urls)
    await set_syslog_services(disco_host, syslog_urls)
