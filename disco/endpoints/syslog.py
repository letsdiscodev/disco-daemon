import logging
from enum import Enum
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key
from disco.endpoints.dependencies import get_db
from disco.models import ApiKey
from disco.utils.syslog import add_syslog_url, get_syslog_urls, remove_syslog_url

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key)])


class SyslogAction(Enum):
    add = "add"
    remove = "remove"


# TODO proper validation
class AddRemoveSyslog(BaseModel):
    action: SyslogAction
    url: str


@router.post("/syslog")
def syslog_post(
    dbsession: Annotated[DBSession, Depends(get_db)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    add_remove_syslog: AddRemoveSyslog,
):
    if add_remove_syslog.action == "add":
        urls = add_syslog_url(dbsession, add_remove_syslog.url, api_key)
    else:
        assert add_remove_syslog.action == "remove"
        urls = remove_syslog_url(dbsession, add_remove_syslog.url, api_key)
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
