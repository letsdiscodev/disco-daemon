import asyncio
import json
import logging
import random
from collections import Counter

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sse_starlette import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key_wo_tx
from disco.models.db import ReadSession
from disco.utils import docker
from disco.utils.logs import (
    STREAM_QUEUE_MAX,
    LogObject,
    LogStreamServer,
    history_key,
    monitor_syslog,
    read_history,
    release_syslog,
    remove_log_collector,
    start_log_collector,
)
from disco.utils.projects import get_project_by_name
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
    config = render_streaming_config(port)
    collector_name = f"disco-syslog-{port}"
    log_queue: asyncio.Queue[LogObject] = asyncio.Queue(maxsize=STREAM_QUEUE_MAX)
    server = LogStreamServer(
        port=port,
        log_queue=log_queue,
        project_name=project_name,
        service_name=service_name,
    )
    await server.start()
    await monitor_syslog(collector_name)
    try:
        await start_log_collector(collector_name, config)
    except BaseException:
        # includes the client leaving during creation (CancelledError)
        await release_syslog(collector_name)
        _cleanups.add(
            asyncio.get_running_loop().create_task(remove_log_collector(collector_name))
        )
        await server.close()
        raise
    try:
        # History is read once every node's collector is connected, so that a
        # line is either in the history or in the live stream. Lines in both
        # are sent once, live lines older than the history are dropped.
        nodes = await docker.get_node_count()
        if not await server.wait_for_connections(nodes, timeout=60):
            log.warning(
                "Fewer than %d log collectors connected within 60s on port %d",
                nodes,
                port,
            )
        history = await read_history(project_name, service_name)
        seen: Counter[tuple[str, str, str]] = Counter(
            history_key(log_obj) for log_obj in history
        )
        last_ts = str(history[-1]["timestamp"]) if history else ""
        for log_obj in history:
            yield ServerSentEvent(event="output", data=json.dumps(log_obj))
        while True:
            log_obj = await log_queue.get()
            key = history_key(log_obj)
            if seen[key] > 0:
                seen[key] -= 1
                continue
            if last_ts and str(log_obj["timestamp"]) < last_ts:
                continue
            yield ServerSentEvent(event="output", data=json.dumps(log_obj))
    finally:
        log.info("HTTP Connection for logs disconnected")
        await release_syslog(collector_name)
        # Not a BackgroundTasks task: those don't run when a streaming client
        # goes away
        _cleanups.add(
            asyncio.get_running_loop().create_task(remove_log_collector(collector_name))
        )
        try:
            await server.close()
        except Exception:
            log.exception("Exception closing log stream server")
        for task in list(_cleanups):
            if task.done():
                _cleanups.discard(task)
