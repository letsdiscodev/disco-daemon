import asyncio
import json
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm.session import Session as DBSession
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key, get_api_key_wo_tx
from disco.endpoints.dependencies import get_db, get_project_from_url
from disco.models import ApiKey, Project
from disco.models.db import Session
from disco.utils import commandoutputs
from disco.utils.commandruns import create_command_run, get_command_run_by_number
from disco.utils.deployments import get_live_deployment
from disco.utils.discofile import DiscoFile, ServiceType
from disco.utils.projects import get_project_by_name

log = logging.getLogger(__name__)

router = APIRouter()


# TODO proper validation
class RunReqBody(BaseModel):
    command: str
    service: str | None
    timeout: int


@router.post(
    "/projects/{project_name}/runs",
    status_code=202,
    dependencies=[Depends(get_api_key)],
)
def run_post(
    dbsession: Annotated[DBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    req_body: RunReqBody,
    background_tasks: BackgroundTasks,
):
    deployment = get_live_deployment(dbsession, project)
    if deployment is None:
        raise HTTPException(422)
    disco_file: DiscoFile = DiscoFile.model_validate_json(deployment.disco_file)
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
            # TODO do in validation instead?
            raise HTTPException(422)
        if disco_file.services[req_body.service].type not in [
            ServiceType.container,
            ServiceType.command,
        ]:
            # TODO do in validation instead?
            raise HTTPException(422, f"Service {req_body.service} can't run commands")
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
    project_name: str, run_number: int, after: datetime | None = None
):
    with Session() as dbsession:
        with dbsession.begin():
            project = get_project_by_name(dbsession, project_name)
            if project is None:
                raise HTTPException(status_code=404)
            run = get_command_run_by_number(dbsession, project, run_number)
            if run is None:
                raise HTTPException(status_code=404)
            source = f"RUN_{run.id}"

    # TODO refactor, this is copy-pasted from deployment output
    async def get_run_output(source: str, after: datetime | None):
        while True:
            with Session() as dbsession:
                with dbsession.begin():
                    output = commandoutputs.get_next(dbsession, source, after=after)
                    if output is not None:
                        if output.text is None:
                            return
                        after = output.created
                        yield json.dumps(
                            {
                                "timestamp": output.created.isoformat(),
                                "text": output.text,
                            }
                        )
            if output is None:
                await asyncio.sleep(0.1)

    return EventSourceResponse(get_run_output(source, after))
