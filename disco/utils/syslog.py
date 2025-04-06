import json
import logging
from typing import Literal, TypedDict

from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey
from disco.utils import keyvalues

log = logging.getLogger(__name__)

SYSLOG_URLS_KEY = "SYSLOG_URLS"


class SyslogUrl(TypedDict):
    url: str
    type: Literal["CORE", "GLOBAL"]


def add_syslog_url(
    dbsession: DBSession, url: str, by_api_key: ApiKey
) -> list[SyslogUrl]:
    syslog_urls = get_syslog_urls(dbsession)
    if url not in [syslog_url["url"] for syslog_url in syslog_urls]:
        log.info("Adding syslog URL %s by %s", url, by_api_key.log())
        syslog_urls.append(
            {
                "url": url,
                "type": "GLOBAL",
            }
        )
    _save_syslog_urls(dbsession, syslog_urls)
    return syslog_urls


def remove_syslog_url(
    dbsession: DBSession, url: str, by_api_key: ApiKey
) -> list[SyslogUrl]:
    syslog_urls = get_syslog_urls(dbsession)
    if url in [syslog_url["url"] for syslog_url in syslog_urls]:
        log.info("Removing syslog URL %s by %s", url, by_api_key.log())
        syslog_urls.remove(
            {
                "url": url,
                "type": "GLOBAL",
            }
        )
    _save_syslog_urls(dbsession, syslog_urls)
    return syslog_urls


def get_syslog_urls(dbsession: DBSession) -> list[SyslogUrl]:
    urls_str = keyvalues.get_value_sync(dbsession, SYSLOG_URLS_KEY)
    if urls_str is None:
        urls_str = "[]"
    syslog_urls = json.loads(urls_str)
    return syslog_urls


def set_core_syslogs(dbsession: DBSession, urls: list[str]) -> list[SyslogUrl]:
    syslog_urls = get_syslog_urls(dbsession)
    other_syslog_urls = [
        syslog_url for syslog_url in syslog_urls if syslog_url["type"] != "CORE"
    ]
    core_syslog_urls: list[SyslogUrl] = [{"url": url, "type": "CORE"} for url in urls]
    new_syslog_urls = core_syslog_urls + other_syslog_urls
    _save_syslog_urls(dbsession, new_syslog_urls)
    return new_syslog_urls


def _save_syslog_urls(dbsession: DBSession, urls: list[SyslogUrl]) -> None:
    keyvalues.set_value_sync(dbsession, SYSLOG_URLS_KEY, json.dumps(urls))


def logspout_url(syslog_url: SyslogUrl) -> str:
    if syslog_url["type"] == "CORE":
        return f"{syslog_url['url']}?filter.labels=disco.log.core:true"
    assert syslog_url["type"] == "GLOBAL"
    return syslog_url["url"]
