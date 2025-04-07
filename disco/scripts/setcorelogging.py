"""Set core syslogs."""

import asyncio
import logging
import sys

from disco.models.db import AsyncSession
from disco.utils import keyvalues
from disco.utils.syslog import set_core_syslogs, set_syslog_services

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    urls = sys.argv[1:]
    asyncio.run(main_async(urls))


async def main_async(urls: list[str]) -> None:
    async with AsyncSession.begin() as dbsession:
        disco_host = await keyvalues.get_value_str(dbsession, "DISCO_HOST")
        syslog_urls = await set_core_syslogs(dbsession, urls)
    await set_syslog_services(disco_host, syslog_urls)
