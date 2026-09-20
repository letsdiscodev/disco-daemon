import logging
from enum import Enum
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from disco.auth import get_api_key_wo_tx
from disco.models.db import ReadSession, Session
from disco.utils import keyvalues
from disco.utils.apikeys import get_valid_api_key_by_id
from disco.utils.syslog import (
    MAX_DESTINATIONS,
    add_syslog_url,
    get_syslog_urls,
    remove_syslog_url,
    set_syslog_services,
)
from disco.utils.vectorconfig import InvalidSyslogUrl, parse_syslog_url

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])


class SyslogAction(Enum):
    add = "add"
    remove = "remove"


class AddRemoveSyslogReqBody(BaseModel):
    action: SyslogAction
    url: str = Field(..., pattern=r"^syslog(\+tls)?://\S+:\d+$")

    @model_validator(mode="after")
    def _valid_syslog_url(self) -> "AddRemoveSyslogReqBody":
        # Strict on add only: a URL stored before 0.34.0 must stay removable
        if self.action == SyslogAction.add:
            try:
                parse_syslog_url(self.url)
            except InvalidSyslogUrl as e:
                raise ValueError(str(e))
        return self


@router.post("/api/syslog")
async def syslog_post(
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    add_remove_syslog: AddRemoveSyslogReqBody,
):
    async with Session.begin() as dbsession:
        api_key = await get_valid_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        if add_remove_syslog.action == SyslogAction.add:
            current = await get_syslog_urls(dbsession)
            if len(current) >= MAX_DESTINATIONS and add_remove_syslog.url not in [
                syslog_url["url"] for syslog_url in current
            ]:
                raise HTTPException(
                    status_code=422,
                    detail=f"At most {MAX_DESTINATIONS} log destinations",
                )
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
async def syslog_get():
    async with ReadSession.begin() as dbsession:
        syslog_urls = await get_syslog_urls(dbsession)
    return {
        "urls": [
            syslog_url["url"]
            for syslog_url in syslog_urls
            if syslog_url["type"] != "CORE"
        ],
    }
