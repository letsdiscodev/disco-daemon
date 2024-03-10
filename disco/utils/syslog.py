import json
import logging

from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey
from disco.utils import keyvalues

log = logging.getLogger(__name__)

SYSLOG_URLS_KEY = "SYSLOG_URLS"


def add_syslog_url(dbsession: DBSession, url: str, by_api_key: ApiKey) -> list[str]:
    urls = get_syslog_urls(dbsession)
    if url not in urls:
        log.info("Adding syslog URL %s by %s", url, by_api_key.log())
        urls.append(url)
    _save_syslog_urls(dbsession, urls)
    return urls


def remove_syslog_url(dbsession: DBSession, url: str, by_api_key: ApiKey) -> list[str]:
    urls = get_syslog_urls(dbsession)
    if url in urls:
        log.info("Removing syslog URL %s by %s", url, by_api_key.log())
        urls.remove(url)
    _save_syslog_urls(dbsession, urls)
    return urls


def get_syslog_urls(dbsession: DBSession) -> list[str]:
    urls_str = keyvalues.get_value_sync(dbsession, SYSLOG_URLS_KEY)
    if urls_str is None:
        urls_str = "[]"
    urls = json.loads(urls_str)
    return urls


def _save_syslog_urls(dbsession: DBSession, urls: list[str]) -> None:
    keyvalues.set_value(dbsession, SYSLOG_URLS_KEY, json.dumps(urls))
