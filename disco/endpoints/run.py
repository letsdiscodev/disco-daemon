import asyncio
import json
import logging
from datetime import datetime
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sse_starlette import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key, get_api_key_wo_tx
from disco.endpoints.dependencies import get_db, get_project_from_url
from disco.models import ApiKey, Project
from disco.models.db import AsyncSession, Session
from disco.utils import commandoutputs, docker
from disco.utils.commandruns import (
    create_command_run,
    get_command_run_by_id,
    get_command_run_by_number,
)
from disco.utils.deployments import get_live_deployment
from disco.utils.discofile import DiscoFile, ServiceType, get_disco_file_from_str
from disco.utils.projects import get_project_by_name_sync

log = logging.getLogger(__name__)

router = APIRouter()


class RunReqBody(BaseModel):
    command: str = Field(..., max_length=4000)
    service: str | None
    timeout: int
    interactive: bool = False


@router.post(
    "/api/projects/{project_name}/runs",
    status_code=202,
)
async def run_post(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    req_body: RunReqBody,
    background_tasks: BackgroundTasks,
):
    deployment = await get_live_deployment(dbsession, project)
    if deployment is None:
        raise HTTPException(422, "Must deploy first")
    disco_file: DiscoFile = get_disco_file_from_str(deployment.disco_file)
    if req_body.service is None:
        if len(list(disco_file.services.keys())) == 0:
            raise HTTPException(422)
        if (
            "web" in disco_file.services
            and disco_file.services["web"].type != ServiceType.static
        ):
            service = "web"
        else:
            services = list(
                [
                    name
                    for name, service in disco_file.services.items()
                    if service.type != ServiceType.static
                ]
            )
            if len(services) == 0:
                raise HTTPException(422, "No service can run commands in project")
            service = services[0]
    else:
        if req_body.service not in disco_file.services:
            raise RequestValidationError(
                errors=(
                    ValidationError.from_exception_data(
                        "ValueError",
                        [
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "value_error",
                                    f'Service "{req_body.service}" not in Discofile: {list(disco_file.services.keys())}',
                                ),
                                loc=("body", "service"),
                                input=req_body.service,
                            )
                        ],
                    )
                ).errors()
            )
        if disco_file.services[req_body.service].type == ServiceType.static:
            raise RequestValidationError(
                errors=(
                    ValidationError.from_exception_data(
                        "ValueError",
                        [
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "value_error",
                                    f'Service "{req_body.service}" can\'t run commands',
                                ),
                                loc=("body", "service"),
                                input=req_body.service,
                            )
                        ],
                    )
                ).errors()
            )
        service = req_body.service
    command_run, func = await create_command_run(
        dbsession=dbsession,
        project=project,
        deployment=deployment,
        service=service,
        command=req_body.command,
        timeout=req_body.timeout,
        interactive=req_body.interactive,
        by_api_key=api_key,
    )
    background_tasks.add_task(func)
    return {
        "run": {
            "id": command_run.id,
            "number": command_run.number,
        },
    }


# deprecated, kept for backward compatibility with older CLI
@router.get(
    "/api/projects/{project_name}/runs/{run_number}/output",
    dependencies=[Depends(get_api_key_wo_tx)],
)
async def run_output_get(
    project_name: str,
    run_number: int,
    last_event_id: Annotated[str | None, Header()] = None,
):
    with Session.begin() as dbsession:
        project = get_project_by_name_sync(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
        run = get_command_run_by_number(dbsession, project, run_number)
        if run is None:
            raise HTTPException(status_code=404)
        after = None
        source = commandoutputs.run_source(run.id)
        if last_event_id is not None:
            output = await commandoutputs.get_by_id(source, last_event_id)
            if output is not None:
                after = output.created

    async def get_run_output(source: str, after: datetime | None):
        while True:
            output = await commandoutputs.get_next(source, after=after)
            if output is not None:
                if output.text is None:
                    yield ServerSentEvent(
                        id=output.id,
                        event="end",
                        data="",
                    )
                    return
                after = output.created
                yield ServerSentEvent(
                    id=output.id,
                    event="output",
                    data=json.dumps(
                        {
                            "timestamp": output.created.isoformat(),
                            "text": output.text,
                        }
                    ),
                )
            if output is None:
                await asyncio.sleep(0.1)

    return EventSourceResponse(get_run_output(source, after))


@router.websocket("/api/projects/{project_name}/runs/{run_id}/ws")
async def asdasdasdasdasdasd(websocket: WebSocket, project_name: str, run_id: str):
    log.info("Websocket function")
    async with AsyncSession.begin() as dbsession:
        run = await get_command_run_by_id(dbsession, run_id)
        if run is None:
            return
        run_number = run.number
    try:
        await websocket.accept()
        log.info("websocket.accept() %s", run_number)
        name = f"{project_name}-run.{run_number}"
        args = [
            "docker",
            "container",
            "start",
            "--attach",
            "--interactive",
            name,
        ]
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
        )

        async def write_stdin() -> None:
            assert process.stdin is not None
            while True:
                chunk = await websocket.receive_bytes()
                process.stdin.write(chunk)

        async def read_stdout() -> None:
            assert process.stdout is not None

            while True:
                chunk = await process.stdout.read(1024)
                if chunk is None:
                    return
                await websocket.send_bytes(b"o:" + chunk)

        async def read_stderr() -> None:
            assert process.stderr is not None

            while True:
                chunk = await process.stderr.read(1024)
                if chunk is None:
                    return
                await websocket.send_bytes(b"e:" + chunk)

        tasks = [
            asyncio.create_task(write_stdin()),
            asyncio.create_task(read_stdout()),
            asyncio.create_task(read_stderr()),
        ]
        gather_stdio_future = asyncio.gather(*tasks)
        process_task = asyncio.create_task(process.wait())
        tasks_to_wait: set[asyncio.Future[Any]] = {gather_stdio_future, process_task}
        try:
            async with asyncio.timeout(86400):
                await asyncio.wait(tasks_to_wait, return_when=asyncio.FIRST_COMPLETED)
                await process.wait()
                await websocket.send_bytes(
                    b"s:" + str(process.returncode).encode("utf-8")
                )
        except TimeoutError:
            process.terminate()
            raise
    except WebSocketDisconnect:
        process.terminate()
    finally:
        await docker.remove_container(name)
