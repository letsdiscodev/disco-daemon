import logging
from enum import Enum
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from disco.auth import get_api_key_wo_tx
from disco.endpoints.dependencies import get_db
from disco.models.db import AsyncSession
from disco.utils import keyvalues
from disco.utils.apikeys import get_valid_api_key_by_id
from disco.utils.syslog import (
    add_syslog_url,
    get_syslog_urls,
    remove_syslog_url,
    set_syslog_services,
)

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])


class SyslogAction(Enum):
    add = "add"
    remove = "remove"


class AddRemoveSyslogReqBody(BaseModel):
    action: SyslogAction
    url: str = Field(..., pattern=r"^syslog(\+tls)?://\S+:\d+$")


@router.post("/api/syslog")
async def syslog_post(
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    add_remove_syslog: AddRemoveSyslogReqBody,
):
    async with AsyncSession.begin() as dbsession:
        api_key = await get_valid_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        if add_remove_syslog.action == SyslogAction.add:
            syslog_urls = await add_syslog_url(
                dbsession, add_remove_syslog.url, api_key
            )
        else:
            assert add_remove_syslog.action == SyslogAction.remove
            syslog_urls = await remove_syslog_url(
                dbsession, add_remove_syslog.url, api_key
            )
        disco_host = await keyvalues.get_value_str(dbsession, "DISCO_HOST")
    await set_syslog_services(disco_host=disco_host, syslog_urls=syslog_urls)
    return {
        "urls": [
            syslog_url["url"]
            for syslog_url in syslog_urls
            if syslog_url["type"] != "CORE"
        ],
    }


@router.get("/api/syslog")
async def syslog_get(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
):
    syslog_urls = await get_syslog_urls(dbsession)
    return {
        "urls": [
            syslog_url["url"]
            for syslog_url in syslog_urls
            if syslog_url["type"] != "CORE"
        ],
    }
