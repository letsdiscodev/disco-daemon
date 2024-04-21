import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key
from disco.endpoints.dependencies import get_db
from disco.utils import docker, keyvalues

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key)])


@router.get("/disco/swarm/join-token")
def join_token_get(dbsession: Annotated[DBSession, Depends(get_db)]):
    return {
        "joinToken": docker.get_swarm_join_token(),
        "ip": keyvalues.get_value(dbsession, "DISCO_ADVERTISE_ADDR"),
    }
