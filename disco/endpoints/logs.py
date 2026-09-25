import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key_wo_tx
from disco.models.db import ReadSession
from disco.utils import docker
from disco.utils.logs import (
    COLLECTOR_CONNECT_TIMEOUT_SECONDS,
    LogSession,
    ensure_log_collector,
    log_listener,
)
from disco.utils.projects import get_project_by_name

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])


@router.get("/api/logs")
async def logs_all():
    return EventSourceResponse(read_logs(project_name=None, service_name=None))


@router.get("/api/logs/{project_name}")
async def logs_project(project_name: str):
    async with ReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
    return EventSourceResponse(read_logs(project_name=project_name, service_name=None))


@router.get("/api/logs/{project_name}/{service_name}")
async def logs_project_service(project_name: str, service_name: str):
    async with ReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
    return EventSourceResponse(
        read_logs(project_name=project_name, service_name=service_name)
    )


async def read_logs(project_name: str | None, service_name: str | None):
    session = LogSession(project_name, service_name)
    log_listener.subscribe(session)
    try:
        await ensure_log_collector()
        nodes = await docker.schedulable_nodes()
        if not await log_listener.wait_for_nodes(
            len(nodes), timeout=COLLECTOR_CONNECT_TIMEOUT_SECONDS
        ):
            log.warning(
                "The log collector is connected from %d of %d nodes",
                log_listener.connected_nodes(),
                len(nodes),
            )
        while True:
            log_line = await session.get()
            yield ServerSentEvent(event="output", data=json.dumps(log_line))
    finally:
        log.info("HTTP Connection for logs disconnected")
        log_listener.unsubscribe(session)
