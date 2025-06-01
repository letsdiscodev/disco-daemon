import asyncio
from asyncio import subprocess
import json
import logging
from datetime import datetime
import shlex
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from sqlalchemy.orm.session import Session as DBSession
from sse_starlette import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key_sync, get_api_key_wo_tx
from disco.endpoints.dependencies import get_db_sync, get_project_from_url_sync
from disco.errors import ProcessStatusError
from disco.models import ApiKey, Project
from disco.models.db import AsyncSession, Session
from disco.utils import commandoutputs, docker, keyvalues
from disco.utils.commandruns import create_command_run, get_command_run_by_number
from disco.utils.deployments import get_live_deployment, get_live_deployment_sync
from disco.utils.discofile import DiscoFile, ServiceType, get_disco_file_from_str
from disco.utils.encryption import decrypt
from disco.utils.projects import get_project_by_name, get_project_by_name_sync, volume_name_for_project
from disco.utils.subprocess import decode_text

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
    dependencies=[Depends(get_api_key_sync)],
)
def run_post(
    dbsession: Annotated[DBSession, Depends(get_db_sync)],
    project: Annotated[Project, Depends(get_project_from_url_sync)],
    api_key: Annotated[ApiKey, Depends(get_api_key_sync)],
    req_body: RunReqBody,
    background_tasks: BackgroundTasks,
):
    deployment = get_live_deployment_sync(dbsession, project)
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
    command_run, func = create_command_run(
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
            "number": command_run.number,
        },
    }


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

    # TODO refactor, this is copy-pasted from deployment output
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






@router.websocket("/api/projects/{project_name}/runs-ws")
async def websocket_endpoint(websocket: WebSocket, project_name: str):
    log.info("Got websocket request")
    try:
        await websocket.accept()
        async with AsyncSession.begin() as dbsession:
            project = await get_project_by_name(dbsession, project_name)
            deployment = await get_live_deployment(dbsession, project)
            registry_host = await keyvalues.get_value(dbsession, "REGISTRY_HOST")

            disco_file: DiscoFile = get_disco_file_from_str(deployment.disco_file)
            assert deployment.status == "COMPLETE"
            service = "web"
            assert service in disco_file.services
            image = docker.get_image_name_for_service(
                disco_file=disco_file,
                service_name=service,
                registry_host=registry_host,
                project_name=project.name,
                deployment_number=deployment.number,
            )
            project_name = project.name
            command = "bash"
            env_variables = [
                (env_var.name, decrypt(env_var.value)) for env_var in await deployment.awaitable_attrs.env_variables
            ]
            env_variables += [
                ("DISCO_PROJECT_NAME", project_name),
                ("DISCO_SERVICE_NAME", service),
                ("DISCO_HOST", await keyvalues.get_value_str(dbsession, "DISCO_HOST")),
                ("DISCO_DEPLOYMENT_NUMBER", str(deployment.number)),
            ]
            if deployment.commit_hash is not None:
                env_variables += [
                    ("DISCO_COMMIT", deployment.commit_hash),
                ]

            network = docker.deployment_network_name(project.name, deployment.number)
            volumes = [
                ("volume", volume_name_for_project(v.name, project.id), v.destination_path)
                for v in disco_file.services[service].volumes
            ]
            more_args = []
            for var_name, var_value in env_variables:
                more_args.append("--env")
                more_args.append(f"{var_name}={var_value}")
            for volume_type, source, destination in volumes:
                assert volume_type in ["bind", "volume"]
                more_args.append("--mount")
                more_args.append(
                    f"type={volume_type},source={source},destination={destination}"
                )
            # if workdir is not None:
            #     more_args.append("--workdir")
            #     more_args.append(workdir)
            name = "interactiverun" # TODO
            timeout = 300 # TODO
            networks = [network]
            args = [
                "docker",
                "container",
                "create",
                "--name",
                name,
                "--label",
                f"disco.project.name={project_name}",
                "--label",
                f"disco.service.name={name}",
                "--log-driver",
                "json-file",
                "--log-opt",
                "max-size=20m",
                "--log-opt",
                "max-file=5",
                "--interactive",
                "--tty",
                *more_args,
                image,
                *(shlex.split(command) if command is not None else []),
            ]
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        async def read_create_container_stdout() -> None:
            assert process.stdout is not None
            async for line in process.stdout:
                line_text = decode_text(line)
                if line_text.endswith("\n"):
                    line_text = line_text[:-1]
                log.info("Output: %s", line_text)

        try:
            async with asyncio.timeout(timeout):
                await asyncio.wait_for(read_create_container_stdout(), timeout)
        except TimeoutError:
            process.terminate()
            raise

        await process.wait()
        if process.returncode != 0:
            raise ProcessStatusError(status=process.returncode)
        for network in networks:
            await docker.add_network_to_container(container=name, network=network)
        more_args = []
        args = [
            "docker",
            "container",
            "start",
            "--attach",
            "--interactive",
            *more_args,
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

        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(*tasks)
        except TimeoutError:
            process.terminate()
            raise

        await process.wait()
        # if process.returncode != 0:
        #     raise docker.CommandRunProcessStatusError(status=process.returncode)

    except WebSocketDisconnect:
        process.terminate()
    finally:
        await docker.remove_container(name)
