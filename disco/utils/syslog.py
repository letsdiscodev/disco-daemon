import json
import logging

from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey
from disco.utils import keyvalues
from disco.utils.mq.tasks import enqueue_task_deprecated

log = logging.getLogger(__name__)

SYSLOG_URLS_KEY = "SYSLOG_URLS"


def add_syslog_url(dbsession: DBSession, url: str, by_api_key: ApiKey) -> list[str]:
    log.info("Adding syslog URL %s by %s", url, by_api_key.log())
    urls = get_syslog_urls(dbsession)
    urls.append(url)
    _save_syslog_urls(dbsession, urls)
    _enqueue_set_syslog_service(dbsession)
    return urls


def remove_syslog_url(dbsession: DBSession, url: str, by_api_key: ApiKey) -> list[str]:
    log.info("Removing syslog URL %s by %s", url, by_api_key.log())
    urls = get_syslog_urls(dbsession)
    urls.remove(url)
    _save_syslog_urls(dbsession, urls)
    _enqueue_set_syslog_service(dbsession)
    return urls


def get_syslog_urls(dbsession: DBSession) -> list[str]:
    urls_str = keyvalues.get_value(dbsession, SYSLOG_URLS_KEY)
    if urls_str is None:
        urls_str = "[]"
    urls = json.loads(urls_str)
    return urls


def _save_syslog_urls(dbsession: DBSession, urls: list[str]) -> None:
    keyvalues.set_value(dbsession, SYSLOG_URLS_KEY, json.dumps(urls))


def _enqueue_set_syslog_service(dbsession: DBSession):
    enqueue_task_deprecated(
        task_name="SET_SYSLOG_SERVICE",
        body=dict(),
    )
