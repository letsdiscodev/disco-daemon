from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm.session import Session as DBSession

from disco.endpoints.dependencies import get_db
from disco.models.db import Session
from disco.utils.auth import get_valid_api_key_by_id

security = HTTPBasic()


def get_api_key(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    dbsession: Annotated[DBSession, Depends(get_db)],
):
    api_key = get_valid_api_key_by_id(dbsession, credentials.username)
    if api_key is None:
        raise HTTPException(status_code=403)
    yield api_key


def get_api_key_wo_tx(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
):
    api_key_id = None
    with Session() as dbsession:
        with dbsession.begin():
            api_key = get_valid_api_key_by_id(dbsession, credentials.username)
            if api_key is None:
                raise HTTPException(status_code=403)
            api_key_id = api_key.id
    yield api_key_id
