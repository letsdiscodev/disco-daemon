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


MAX_DESTINATIONS = 10

LOGGING_DESTINATION_BUFFER_KEY = "LOGGING_DESTINATION_BUFFER_BYTES"
LOGGING_STREAM_BUFFER_KEY = "LOGGING_STREAM_BUFFER_BYTES"

_reconcile_lock = asyncio.Lock()


async def get_destination_buffer_bytes(dbsession: DBSession) -> int:
    value = await keyvalues.get_value(dbsession, LOGGING_DESTINATION_BUFFER_KEY)
    if value is None:
        return vectorconfig.DEFAULT_DESTINATION_BUFFER_BYTES
    return int(value)


async def get_stream_buffer_bytes(dbsession: DBSession) -> int:
    value = await keyvalues.get_value(dbsession, LOGGING_STREAM_BUFFER_KEY)
    if value is None:
        return vectorconfig.DEFAULT_STREAM_BUFFER_BYTES
    return int(value)


async def set_syslog_services(
    disco_host: str,
    syslog_urls: list[SyslogUrl],
    buffer_bytes: int = vectorconfig.DEFAULT_DESTINATION_BUFFER_BYTES,
) -> None:
    # A collector's service name is derived from its rendered config (which
    # includes the hostname and buffer size). Anything running under another
    # name (logspout, an older config, a removed destination) is replaced.
    # New collectors are created before the old ones are removed.
    async with _reconcile_lock:
        existing = await docker.list_syslog_services()
        desired: dict[str, SyslogUrl] = {}
        untouched: set[tuple[str, str]] = set()
        for syslog_url in syslog_urls:
            try:
                config = vectorconfig.render_syslog_config(
                    syslog_url["url"], syslog_url["type"], buffer_bytes, disco_host
                )
            except vectorconfig.InvalidSyslogUrl as e:
                # Stored before 0.34.0, leave whatever runs for it alone
                log.warning(
                    "Leaving the collector of %s as it is: %s", syslog_url["url"], e
                )
                untouched.add((syslog_url["url"], syslog_url["type"]))
                continue
            name = docker.syslog_service_name(
                syslog_url["url"], syslog_url["type"], config
            )
            desired[name] = syslog_url
        existing_names = {service.name for service in existing}
        for name, syslog_url in desired.items():
            if name not in existing_names:
                await docker.start_syslog_service(
                    disco_host=disco_host,
                    url=syslog_url["url"],
                    type=syslog_url["type"],
                    buffer_bytes=buffer_bytes,
                )
        to_remove = [
            service
            for service in existing
            if service.name not in desired
            and (service.url, service.type) not in untouched
        ]
        not_ready: set[tuple[str, str]] = set()
        replaced = {(s.url, s.type) for s in to_remove}
        replacements = [
            name
            for name, syslog_url in desired.items()
            if (syslog_url["url"], syslog_url["type"]) in replaced
        ]
        if replacements:
            for name in replacements:
                if not await docker.wait_for_global_service(name, timeout=180):
                    syslog_url = desired[name]
                    not_ready.add((syslog_url["url"], syslog_url["type"]))
            await asyncio.sleep(docker.COLLECTOR_SETTLE_SECONDS)
        for service in to_remove:
            if (service.url, service.type) in not_ready:
                log.warning(
                    "Keeping %s: its replacement is not running on every node",
                    service.name,
                )
                continue
            await docker.rm_syslog_service(service)
        await docker.prune_logging_configs()


async def reconcile_syslog_services_on_disco_boot() -> None:
    from disco.models.db import ReadSession
    from disco.utils.logs import remove_all_log_collectors

    try:
        await remove_all_log_collectors()
        async with ReadSession.begin() as dbsession:
            disco_host = await keyvalues.get_value_str(dbsession, "DISCO_HOST")
            syslog_urls = await get_syslog_urls(dbsession)
            buffer_bytes = await get_destination_buffer_bytes(dbsession)
        await set_syslog_services(disco_host, syslog_urls, buffer_bytes)
    except Exception:
        log.exception("Failed to reconcile syslog services on boot")
