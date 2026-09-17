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


_buffer_bytes_cache = {"value": vectorconfig.DEFAULT_DESTINATION_BUFFER_BYTES}


def get_destination_buffer_bytes_sync() -> int:
    """the last value read from the db by the reconciler (for callers without a db
    session, the hostname update)."""
    return _buffer_bytes_cache["value"]


def _desired_service_name(
    url: str, type: Literal["CORE", "GLOBAL"], buffer_bytes: int, disco_host: str
) -> str:
    config = vectorconfig.render_syslog_config(url, type, buffer_bytes, disco_host)
    return docker.syslog_service_name(url, type, config)


async def set_syslog_services(
    disco_host: str,
    syslog_urls: list[SyslogUrl],
    buffer_bytes: int = vectorconfig.DEFAULT_DESTINATION_BUFFER_BYTES,
) -> None:
    """make the running collectors match the configured destinations.

    identity of a collector = url + type + rendered config (image, buffer size and
    hostname included) = its service name. a destination whose collector exists under that
    exact name is left alone; anything else (a logspout service from before 0.34.0,
    a collector rendered with an older config, a removed destination) is replaced:
    new services are created BEFORE old ones are removed, so a destination never
    goes without a collector. serialized with a lock so two api calls cannot create
    the same service twice; safe to run at any time, including at daemon startup.
    """
    _buffer_bytes_cache["value"] = buffer_bytes
    async with _reconcile_lock:
        existing = await docker.list_syslog_services()
        desired: dict[str, SyslogUrl] = {}
        for syslog_url in syslog_urls:
            name = _desired_service_name(
                syslog_url["url"], syslog_url["type"], buffer_bytes, disco_host
            )
            desired[name] = syslog_url
        existing_names = {service.name for service in existing}
        for name, syslog_url in desired.items():
            if name in existing_names:
                continue
            await docker.start_syslog_service(
                disco_host=disco_host,
                url=syslog_url["url"],
                type=syslog_url["type"],
                buffer_bytes=buffer_bytes,
            )
        for service in existing:
            if service.name not in desired:
                await docker.rm_syslog_service(service)
        await docker.prune_logging_configs()


async def reconcile_syslog_services_on_boot() -> None:
    """repair a partial state left by a crash between two docker calls."""
    from disco.models.db import ReadSession

    try:
        async with ReadSession.begin() as dbsession:
            disco_host = await keyvalues.get_value_str(dbsession, "DISCO_HOST")
            syslog_urls = await get_syslog_urls(dbsession)
            buffer_bytes = await get_destination_buffer_bytes(dbsession)
        await set_syslog_services(disco_host, syslog_urls, buffer_bytes)
    except Exception:
        log.exception("Failed to reconcile syslog services on boot")
