import asyncio
import json
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from sqlalchemy.orm.session import Session as DBSession
from sse_starlette import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key_sync, get_api_key_wo_tx
from disco.endpoints.dependencies import get_project_from_url, get_sync_db
from disco.models import ApiKey, Project
from disco.models.db import AsyncSession, Session
from disco.utils import commandoutputs
from disco.utils.commandruns import create_command_run, get_command_run_by_number
from disco.utils.deployments import get_live_deployment_sync
from disco.utils.discofile import DiscoFile, ServiceType, get_disco_file_from_str
from disco.utils.projects import get_project_by_name_sync

log = logging.getLogger(__name__)

router = APIRouter()


class RunReqBody(BaseModel):
    command: str = Field(..., max_length=4000)
    service: str | None
    timeout: int


@router.post(
    "/projects/{project_name}/runs",
    status_code=202,
    dependencies=[Depends(get_api_key_sync)],
)
def run_post(
    dbsession: Annotated[DBSession, Depends(get_sync_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
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
            and disco_file.services["web"].type == ServiceType.container
        ):
            service = "web"
        else:
            services = list(
                [
                    name
                    for name, service in disco_file.services.items()
                    if service.type in [ServiceType.container, ServiceType.command]
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
        if disco_file.services[req_body.service].type not in [
            ServiceType.container,
            ServiceType.command,
        ]:
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
        by_api_key=api_key,
    )
    background_tasks.add_task(func)
    return {
        "run": {
            "number": command_run.number,
        },
    }


@router.get(
    "/projects/{project_name}/runs/{run_number}/output",
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
        source = f"RUN_{run.id}"
        after = None
        if last_event_id is not None:
            output = commandoutputs.get_by_id(dbsession, last_event_id)
            if output is not None:
                after = output.created

    # TODO refactor, this is copy-pasted from deployment output
    async def get_run_output(source: str, after: datetime | None):
        while True:
            async with AsyncSession.begin() as dbsession:
                output = await commandoutputs.get_next(dbsession, source, after=after)
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
