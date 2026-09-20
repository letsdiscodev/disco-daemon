import asyncio
import json
import logging
from typing import Literal, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession as DBSession

from disco.models import ApiKey
from disco.utils import docker, keyvalues, vectorconfig

log = logging.getLogger(__name__)

SYSLOG_URLS_KEY = "SYSLOG_URLS"


class SyslogUrl(TypedDict):
    url: str
    type: Literal["CORE", "GLOBAL"]


async def add_syslog_url(
    dbsession: DBSession, url: str, by_api_key: ApiKey
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
    dbsession: DBSession, url: str, by_api_key: ApiKey
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


async def get_syslog_urls(dbsession: DBSession) -> list[SyslogUrl]:
    urls_str = await keyvalues.get_value(dbsession, SYSLOG_URLS_KEY)
    if urls_str is None:
        urls_str = "[]"
    syslog_urls = json.loads(urls_str)
    return syslog_urls


async def set_core_syslogs(dbsession: DBSession, urls: list[str]) -> list[SyslogUrl]:
    log.info("Updating core Syslogs: %s", urls)
    syslog_urls = await get_syslog_urls(dbsession)
    other_syslog_urls = [
        syslog_url for syslog_url in syslog_urls if syslog_url["type"] != "CORE"
    ]
    core_syslog_urls: list[SyslogUrl] = [{"url": url, "type": "CORE"} for url in urls]
    new_syslog_urls = core_syslog_urls + other_syslog_urls
    await _save_syslog_urls(dbsession, new_syslog_urls)
    return new_syslog_urls


async def _save_syslog_urls(dbsession: DBSession, syslog_urls: list[SyslogUrl]) -> None:
    await keyvalues.set_value(dbsession, SYSLOG_URLS_KEY, json.dumps(syslog_urls))


_reconcile_lock = asyncio.Lock()


async def set_syslog_services(disco_host: str, syslog_urls: list[SyslogUrl]) -> None:
    # A collector whose config changed (hostname, image, config itself) is
    # replaced, with a few seconds without forwarding, as with logspout.
    async with _reconcile_lock:
        desired: dict[str, tuple[SyslogUrl, str]] = {}
        for syslog_url in syslog_urls:
            config = vectorconfig.render_syslog_config(
                syslog_url["url"], syslog_url["type"], disco_host
            )
            name = docker.syslog_service_name(syslog_url["url"], syslog_url["type"])
            desired[name] = (syslog_url, vectorconfig.config_hash(config))
        kept = set()
        extras = []
        for service in await docker.list_syslog_services():
            if service.name in desired and desired[service.name][1] == service.config:
                kept.add(service.name)
            elif service.name in desired:
                log.info("Stopping Syslog service %s (%s)", service.name, service.url)
                await docker.rm_service(service.name)
            else:
                extras.append(service)
        for name, (syslog_url, _) in desired.items():
            if name not in kept:
                await docker.start_syslog_service(
                    disco_host=disco_host,
                    url=syslog_url["url"],
                    type=syslog_url["type"],
                )
        # a collector under another name (logspout) forwards until its replacement runs
        for service in extras:
            log.info("Stopping Syslog service %s (%s)", service.name, service.url)
            await docker.rm_service(service.name)


async def reconcile_syslog_services_on_disco_boot() -> None:
    from disco.models.db import ReadSession

    try:
        if not await docker.image_exists(vectorconfig.VECTOR_IMAGE):
            # a new Vector image: the running collectors forward while it is pulled
            await docker.pull_image_on_all_nodes(vectorconfig.VECTOR_IMAGE)
        async with ReadSession.begin() as dbsession:
            disco_host = await keyvalues.get_value_str(dbsession, "DISCO_HOST")
            syslog_urls = await get_syslog_urls(dbsession)
        await set_syslog_services(disco_host, syslog_urls)
    except Exception:
        log.exception("Failed to reconcile syslog services on boot")
