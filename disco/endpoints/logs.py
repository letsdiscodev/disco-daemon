import asyncio
import json
import logging
import random

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sse_starlette import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key_wo_tx
from disco.models.db import ReadSession
from disco.utils.logs import (
    STREAM_QUEUE_MAX,
    LogObject,
    LogStreamServer,
    history_key,
    monitor_syslog,
    read_history,
    remove_log_collector,
    start_log_collector,
)
from disco.utils.projects import get_project_by_name
from disco.utils.syslog import get_stream_buffer_bytes
from disco.utils.vectorconfig import render_streaming_config

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])

_cleanups: set[asyncio.Task] = set()


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
    async with ReadSession.begin() as dbsession:
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
    async with ReadSession.begin() as dbsession:
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
    async with ReadSession.begin() as dbsession:
        buffer_bytes = await get_stream_buffer_bytes(dbsession)
    config = render_streaming_config(port, buffer_bytes)
    collector_name = f"disco-syslog-{port}"
    log_queue: asyncio.Queue[LogObject] = asyncio.Queue(maxsize=STREAM_QUEUE_MAX)
    server = LogStreamServer(
        port=port,
        log_queue=log_queue,
        project_name=project_name,
        service_name=service_name,
    )
    # listen before the collector exists: the first lines have somewhere to go
    await server.start()
    await monitor_syslog(collector_name)
    try:
        config_name = await start_log_collector(collector_name, config)
    except Exception:
        await server.close()
        raise
    try:
        # the last lines docker retained, then live; lines the collector already
        # streamed while the history was read are not shown twice
        history = await read_history(project_name, service_name)
        seen = {history_key(log_obj) for log_obj in history}
        for log_obj in history:
            yield ServerSentEvent(event="output", data=json.dumps(log_obj))
        while True:
            log_obj = await log_queue.get()
            key = history_key(log_obj)
            if key in seen:
                seen.discard(key)
                continue
            yield ServerSentEvent(
                event="output",
                data=json.dumps(log_obj),
            )
    finally:
        log.info("HTTP Connection for logs disconnected")
        try:
            await server.close()
        except Exception:
            log.exception("Exception closing log stream server")
        # scheduled on the loop directly: the response's background tasks are not run
        # when a streaming client goes away (measured: collectors were left behind)
        _cleanups.add(
            asyncio.get_running_loop().create_task(
                remove_log_collector(collector_name, config_name)
            )
        )
        for task in list(_cleanups):
            if task.done():
                _cleanups.discard(task)
