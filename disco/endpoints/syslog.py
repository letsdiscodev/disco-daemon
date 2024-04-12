import logging
from enum import Enum
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key
from disco.endpoints.dependencies import get_db
from disco.models import ApiKey
from disco.utils import docker, keyvalues
from disco.utils.syslog import add_syslog_url, get_syslog_urls, remove_syslog_url

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key)])


class SyslogAction(Enum):
    add = "add"
    remove = "remove"


class AddRemoveSyslogReqBody(BaseModel):
    action: SyslogAction
    url: str = Field(..., pattern=r"^syslog(\+tls)?://\S+:\d+$")


@router.post("/syslog")
def syslog_post(
    dbsession: Annotated[DBSession, Depends(get_db)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    add_remove_syslog: AddRemoveSyslogReqBody,
    background_tasks: BackgroundTasks,
):
    if add_remove_syslog.action == SyslogAction.add:
        urls = add_syslog_url(dbsession, add_remove_syslog.url, api_key)
    else:
        assert add_remove_syslog.action == SyslogAction.remove
        urls = remove_syslog_url(dbsession, add_remove_syslog.url, api_key)
    disco_host = keyvalues.get_value(dbsession, "DISCO_HOST")
    assert disco_host is not None
    background_tasks.add_task(docker.set_syslog_service, disco_host, urls)
    return {
        "urls": urls,
    }


@router.get("/syslog")
def syslog_get(
    dbsession: Annotated[DBSession, Depends(get_db)],
):
    return {
        "urls": get_syslog_urls(dbsession),
    }
