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
    MAX_STREAMS,
    STREAM_QUEUE_MAX,
    LogObject,
    LogStreamServer,
    for_client,
    get_active_syslogs,
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
_admission_lock = asyncio.Lock()


async def _admit() -> None:
    """the session cap, before the response starts (a real 429), under a lock so two
    requests cannot both be the tenth; counted on this process's own sessions, so
    orphans of a previous process (removed at boot anyway) never block clients."""
    async with _admission_lock:
        if len(await get_active_syslogs()) >= MAX_STREAMS:
            raise HTTPException(
                status_code=429, detail=f"At most {MAX_STREAMS} log sessions at once"
            )


@router.get("/api/logs")
async def logs_all(background_tasks: BackgroundTasks):
    await _admit()
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
    await _admit()
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
    await _admit()
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
    config_name = docker.stream_config_name(config)
    try:
        await start_log_collector(collector_name, config)
    except BaseException:
        # includes the client leaving during creation (CancelledError)
        _cleanups.add(
            asyncio.get_running_loop().create_task(
                remove_log_collector(collector_name, config_name)
            )
        )
        await server.close()
        raise
    try:
        # the last lines docker retained, then live. the history is read once one
        # collector task per node has connected (their docker sources are up by then),
        # so a line lands in the history or in the live stream; lines in both are shown
        # once, and a queued live line older than the history's last line is dropped
        # (it is older than the last 100 lines by definition)
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
        last_ts = (
            str(history[-1].get("ts", history[-1]["timestamp"])) if history else ""
        )
        for log_obj in history:
            yield ServerSentEvent(event="output", data=json.dumps(for_client(log_obj)))
        while True:
            log_obj = await log_queue.get()
            key = history_key(log_obj)
            if seen[key] > 0:
                seen[key] -= 1
                continue
            if last_ts and str(log_obj.get("ts", log_obj["timestamp"])) < last_ts:
                continue
            yield ServerSentEvent(
                event="output",
                data=json.dumps(for_client(log_obj)),
            )
    finally:
        log.info("HTTP Connection for logs disconnected")
        # scheduled on the loop directly, and first: the response's background tasks
        # are not run when a streaming client goes away (measured: collectors were
        # left behind), and closing the server must not stand in the way
        _cleanups.add(
            asyncio.get_running_loop().create_task(
                remove_log_collector(collector_name, config_name)
            )
        )
        try:
            await server.close()
        except Exception:
            log.exception("Exception closing log stream server")
        for task in list(_cleanups):
            if task.done():
                _cleanups.discard(task)
