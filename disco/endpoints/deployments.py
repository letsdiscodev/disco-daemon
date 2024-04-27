import asyncio
import json
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm.session import Session as DBSession
from sse_starlette import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key, get_api_key_wo_tx
from disco.endpoints.dependencies import get_db, get_project_from_url
from disco.models import ApiKey, Project
from disco.models.db import Session
from disco.utils import commandoutputs
from disco.utils.deployments import (
    create_deployment,
    get_deployment_by_number,
    get_last_deployment,
)
from disco.utils.discofile import DiscoFile
from disco.utils.mq.tasks import enqueue_task_deprecated
from disco.utils.projects import get_project_by_name

log = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/projects/{project_name}/deployments",
    dependencies=[Depends(get_api_key)],
)
def deployments_get(
    project: Annotated[Project, Depends(get_project_from_url)],
):
    return {
        "deployments": [
            {
                "number": deployment.number,
                "created": deployment.created.isoformat(),
                "status": deployment.status,
                "commitHash": deployment.commit_hash,
            }
            for deployment in project.deployments
        ]
    }


class DeploymentRequestBody(BaseModel):
    commit: str = Field("_DEPLOY_LATEST_", pattern=r"^\S+$")
    disco_file: DiscoFile | None = Field(None, alias="discoFile")

    @model_validator(mode="after")
    def commit_or_disco_file_required(self) -> "DeploymentRequestBody":
        if self.commit is None and self.disco_file is None:
            raise ValueError("Must provide one of commit or discoFile")
        if (
            self.commit is not None
            and self.commit != "_DEPLOY_LATEST_"
            and self.disco_file is not None
        ):
            raise ValueError("Must provide only one of commit or discoFile")
        return self


def process_deployment(deployment_id: str) -> None:
    enqueue_task_deprecated(
        task_name="PROCESS_DEPLOYMENT",
        body=dict(
            deployment_id=deployment_id,
        ),
    )


@router.post(
    "/projects/{project_name}/deployments",
    status_code=201,
    dependencies=[Depends(get_api_key)],
)
def deployments_post(
    dbsession: Annotated[DBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    req_body: DeploymentRequestBody,
    background_tasks: BackgroundTasks,
):
    deployment = create_deployment(
        dbsession=dbsession,
        project=project,
        commit_hash=req_body.commit if req_body.disco_file is None else None,
        disco_file=req_body.disco_file,
        by_api_key=api_key,
    )
    background_tasks.add_task(process_deployment, deployment.id)
    return {
        "deployment": {
            "number": deployment.number,
        },
    }


@router.get(
    "/projects/{project_name}/deployments/{deployment_number}/output",
    dependencies=[Depends(get_api_key_wo_tx)],
)
async def deployment_output_get(
    project_name: str,
    deployment_number: int,
    last_event_id: Annotated[str | None, Header()] = None,
):
    with Session.begin() as dbsession:
        project = get_project_by_name(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
        if deployment_number == 0:
            deployment = get_last_deployment(dbsession, project)
        else:
            deployment = get_deployment_by_number(dbsession, project, deployment_number)
        if deployment is None:
            raise HTTPException(status_code=404)
        source = f"DEPLOYMENT_{deployment.id}"
        after = None
        if last_event_id is not None:
            output = commandoutputs.get_by_id(dbsession, last_event_id)
            if output is not None:
                after = output.created

    async def get_build_output(source: str, after: datetime | None):
        while True:
            with Session.begin() as dbsession:
                output = commandoutputs.get_next(dbsession, source, after=after)
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

    return EventSourceResponse(get_build_output(source, after))
