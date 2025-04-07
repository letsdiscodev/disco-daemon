import asyncio
import json
import logging
import random

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sse_starlette import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key_wo_tx
from disco.models.db import AsyncSession
from disco.utils import docker
from disco.utils.logs import LOGSPOUT_CMD, JsonLogServer, monitor_syslog
from disco.utils.projects import get_project_by_name

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])


@router.get("/api/logs")
async def logs_all(background_tasks: BackgroundTasks):
    return EventSourceResponse(
        read_logs(
            project_name=None, service_name=None, background_tasks=background_tasks
        )
    )


@router.get("/api/logs/{project_name}")
async def logs_project(
    project_name: str,
    background_tasks: BackgroundTasks,
):
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
    return EventSourceResponse(
        read_logs(
            project_name=project_name,
            service_name=None,
            background_tasks=background_tasks,
        )
    )


@router.get("/api/logs/{project_name}/{service_name}")
async def logs_project_service(
    project_name: str,
    service_name: str,
    background_tasks: BackgroundTasks,
):
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
    return EventSourceResponse(
        read_logs(
            project_name=project_name,
            service_name=service_name,
            background_tasks=background_tasks,
        )
    )


async def read_logs(
    project_name: str | None,
    service_name: str | None,
    background_tasks: BackgroundTasks,
):
    port = random.randint(10000, 65535)
    logspout_cmd = LOGSPOUT_CMD.copy()
    assert logspout_cmd[4] == "{name}"
    syslog_service_name = f"disco-syslog-{port}"
    await monitor_syslog(syslog_service_name)
    logspout_cmd[4] = syslog_service_name
    logspout_cmd[-1] = logspout_cmd[-1].format(port=port)
    transport = None
    log_queue: asyncio.Queue[dict[str, str | dict[str, str]]] = asyncio.Queue()
    await asyncio.create_subprocess_exec(*logspout_cmd)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: JsonLogServer(
            log_queue=log_queue, project_name=project_name, service_name=service_name
        ),
        local_addr=("0.0.0.0", port),
    )
    try:
        while True:
            log_obj = await log_queue.get()
            yield ServerSentEvent(
                event="output",
                data=json.dumps(log_obj),
            )
    finally:
        log.info("HTTP Connection for logs disconnected")
        if transport is not None:
            try:
                transport.close()
                log.info("Closed datagram log endpoint")
            except Exception:
                log.exception("Exception closing transport")
        background_tasks.add_task(docker.rm_service, syslog_service_name)
