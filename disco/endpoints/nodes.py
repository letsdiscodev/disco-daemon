import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key_sync
from disco.endpoints.dependencies import get_sync_db
from disco.utils import docker, keyvalues

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_sync)])


@router.get("/api/disco/swarm/join-token")
def join_token_get(dbsession: Annotated[DBSession, Depends(get_sync_db)]):
    return {
        "joinToken": docker.get_swarm_join_token(),
        "ip": keyvalues.get_value_sync(dbsession, "DISCO_ADVERTISE_ADDR"),
    }
