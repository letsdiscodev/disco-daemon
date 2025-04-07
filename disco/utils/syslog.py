import json
import logging
from typing import Literal, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from disco.models import ApiKey
from disco.utils import docker, keyvalues

log = logging.getLogger(__name__)

SYSLOG_URLS_KEY = "SYSLOG_URLS"


class SyslogUrl(TypedDict):
    url: str
    type: Literal["CORE", "GLOBAL"]


async def add_syslog_url(
    dbsession: AsyncDBSession, url: str, by_api_key: ApiKey
) -> list[SyslogUrl]:
    syslog_urls = await get_syslog_urls(dbsession)
    if url not in [syslog_url["url"] for syslog_url in syslog_urls]:
        log.info("Adding syslog URL %s by %s", url, by_api_key.log())
        syslog_urls.append(
            {
                "url": url,
                "type": "GLOBAL",
            }
        )
    await _save_syslog_urls(dbsession, syslog_urls)
    return syslog_urls


async def remove_syslog_url(
    dbsession: AsyncDBSession, url: str, by_api_key: ApiKey
) -> list[SyslogUrl]:
    syslog_urls = await get_syslog_urls(dbsession)
    if url in [syslog_url["url"] for syslog_url in syslog_urls]:
        log.info("Removing syslog URL %s by %s", url, by_api_key.log())
        syslog_urls.remove(
            {
                "url": url,
                "type": "GLOBAL",
            }
        )
    await _save_syslog_urls(dbsession, syslog_urls)
    return syslog_urls


async def get_syslog_urls(dbsession: AsyncDBSession) -> list[SyslogUrl]:
    urls_str = await keyvalues.get_value(dbsession, SYSLOG_URLS_KEY)
    if urls_str is None:
        urls_str = "[]"
    syslog_urls = json.loads(urls_str)
    return syslog_urls


async def set_core_syslogs(
    dbsession: AsyncDBSession, urls: list[str]
) -> list[SyslogUrl]:
    log.info("Updating core Syslogs: %s", urls)
    syslog_urls = await get_syslog_urls(dbsession)
    other_syslog_urls = [
        syslog_url for syslog_url in syslog_urls if syslog_url["type"] != "CORE"
    ]
    core_syslog_urls: list[SyslogUrl] = [{"url": url, "type": "CORE"} for url in urls]
    new_syslog_urls = core_syslog_urls + other_syslog_urls
    await _save_syslog_urls(dbsession, new_syslog_urls)
    return new_syslog_urls


async def _save_syslog_urls(
    dbsession: AsyncDBSession, syslog_urls: list[SyslogUrl]
) -> None:
    await keyvalues.set_value(dbsession, SYSLOG_URLS_KEY, json.dumps(syslog_urls))


async def set_syslog_services(disco_host: str, syslog_urls: list[SyslogUrl]) -> None:
    existing_services = await docker.list_syslog_services()
    # add missing services
    for syslog_url in syslog_urls:
        already_exists = False
        for existing_service in existing_services:
            if syslog_url["url"] == existing_service.url:
                already_exists = True
        if not already_exists:
            await docker.start_syslog_service(
                disco_host=disco_host,
                url=syslog_url["url"],
                type=syslog_url["type"],
            )
    # remove extra services
    for existing_service in existing_services:
        should_still_exist = False
        for syslog_url in syslog_urls:
            if syslog_url["url"] == existing_service.url:
                should_still_exist = True
        if not should_still_exist:
            log.info("Stopping Syslog service %s", existing_service.url)
            await docker.rm_service(existing_service.name)
