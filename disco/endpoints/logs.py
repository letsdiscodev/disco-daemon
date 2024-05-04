import asyncio
import json
import logging
import random

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sse_starlette import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key_wo_tx
from disco.models.db import Session
from disco.utils import docker
from disco.utils.projects import get_project_by_name_sync

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
    with Session.begin() as dbsession:
        project = get_project_by_name_sync(dbsession, project_name)
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
    with Session.begin() as dbsession:
        project = get_project_by_name_sync(dbsession, project_name)
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
    syslog_service = f"disco-syslog-{port}"
    logspout_cmd[4] = syslog_service
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
        background_tasks.add_task(docker.stop_service_async, syslog_service)


LOGSPOUT_CMD = [
    "docker",
    "service",
    "create",
    "--name",
    "{name}",
    "--mode",
    "global",
    "--env",
    "BACKLOG=false",
    "--env",
    'RAW_FORMAT={ "container" : "{{`{{ .Container.Name }}`}}", '
    '"labels": {{`{{ toJSON .Container.Config.Labels }}`}}, '
    '"timestamp": "{{`{{ .Time.Format "2006-01-02T15:04:05Z07:00" }}`}}", '
    '"message": {{`{{ toJSON .Data }}`}} }',
    "--mount",
    "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
    "--network",
    "disco-logging",
    "--env",
    "ALLOW_TTY=true",
    "gliderlabs/logspout",
    "raw://disco:{port}",
]


class JsonLogServer(asyncio.DatagramProtocol):
    def __init__(
        self,
        log_queue,
        project_name: str | None = None,
        service_name: str | None = None,
    ):
        self.log_queue = log_queue
        self.project_name = project_name
        self.service_name = service_name

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            json_str = data.decode("utf-8")
        except UnicodeDecodeError:
            log.error("Failed to UTF-8 decode log str: %s", data)
            return
        try:
            log_obj = json.loads(json_str)
        except json.decoder.JSONDecodeError:
            log.error("Failed to JSON decode log str: %s", json_str)
            return
        if self.project_name is not None:
            if log_obj["labels"].get("disco.project.name") != self.project_name:
                return
        if self.service_name is not None:
            if log_obj["labels"].get("disco.service.name") != self.service_name:
                return
        self.log_queue.put_nowait(log_obj)

    def connection_lost(self, exception):
        try:
            self.transport.close()
        except Exception:
            pass
