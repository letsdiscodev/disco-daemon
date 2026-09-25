import asyncio
import json
import logging
import random

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key_wo_tx
from disco.models.db import ReadSession
from disco.utils import docker
from disco.utils.logs import LOGSPOUT_CMD, JsonLogServer, LogSession, monitor_syslog
from disco.utils.projects import get_project_by_name

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])

_cleanups: set[asyncio.Task] = set()


@router.get("/api/logs")
async def logs_all():
    return EventSourceResponse(read_logs(project_name=None, service_name=None))


@router.get("/api/logs/{project_name}")
async def logs_project(
    project_name: str,
):
    async with ReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
    return EventSourceResponse(
        read_logs(
            project_name=project_name,
            service_name=None,
        )
    )


@router.get("/api/logs/{project_name}/{service_name}")
async def logs_project_service(
    project_name: str,
    service_name: str,
):
    async with ReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
    return EventSourceResponse(
        read_logs(
            project_name=project_name,
            service_name=service_name,
        )
    )


async def read_logs(
    project_name: str | None,
    service_name: str | None,
):
    port = random.randint(10000, 65535)
    logspout_cmd = LOGSPOUT_CMD.copy()
    assert logspout_cmd[4] == "{name}"
    syslog_service_name = f"disco-syslog-{port}"
    await monitor_syslog(syslog_service_name)
    logspout_cmd[4] = syslog_service_name
    logspout_cmd[-1] = logspout_cmd[-1].format(port=port)
    transport = None
    session = LogSession()
    await asyncio.create_subprocess_exec(*logspout_cmd)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: JsonLogServer(
            session=session, project_name=project_name, service_name=service_name
        ),
        local_addr=("0.0.0.0", port),
    )
    try:
        while True:
            log_obj = await session.get()
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
        task = asyncio.get_running_loop().create_task(
            remove_log_collector(syslog_service_name)
        )
        _cleanups.add(task)
        task.add_done_callback(_cleanups.discard)


async def remove_log_collector(service_name: str) -> None:
    # in case service would still be starting when we're doing the clean up,
    # we run the clean up again for some time.
    try:
        for _ in range(10):
            if await docker.service_exists(service_name):
                await docker.rm_service(service_name)
                return
            await asyncio.sleep(3)
        log.warning("Log collector %s not found, not removed", service_name)
    except Exception:
        log.exception("Failed to remove the log collector %s", service_name)
