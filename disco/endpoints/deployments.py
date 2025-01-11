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

from disco.auth import get_api_key_sync, get_api_key_wo_tx
from disco.endpoints.dependencies import get_project_from_url_sync, get_sync_db
from disco.models import ApiKey, Project
from disco.models.db import AsyncSession
from disco.utils import commandoutputs
from disco.utils.deploymentflow import enqueue_deployment
from disco.utils.deployments import (
    cancel_deployment,
    create_deployment_sync,
    get_deployment_by_number,
    get_last_deployment,
)
from disco.utils.discofile import DiscoFile
from disco.utils.projects import get_project_by_name

log = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/api/projects/{project_name}/deployments",
    dependencies=[Depends(get_api_key_sync)],
)
def deployments_get(
    project: Annotated[Project, Depends(get_project_from_url_sync)],
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


@router.post(
    "/api/projects/{project_name}/deployments",
    status_code=201,
    dependencies=[Depends(get_api_key_sync)],
)
def deployments_post(
    dbsession: Annotated[DBSession, Depends(get_sync_db)],
    project: Annotated[Project, Depends(get_project_from_url_sync)],
    api_key: Annotated[ApiKey, Depends(get_api_key_sync)],
    req_body: DeploymentRequestBody,
    background_tasks: BackgroundTasks,
):
    deployment = create_deployment_sync(
        dbsession=dbsession,
        project=project,
        commit_hash=req_body.commit if req_body.disco_file is None else None,
        disco_file=req_body.disco_file,
        by_api_key=api_key,
    )
    background_tasks.add_task(enqueue_deployment, deployment.id)
    return {
        "deployment": {
            "number": deployment.number,
        },
    }


@router.delete(
    "/api/projects/{project_name}/deployments/{deployment_number}",
    dependencies=[Depends(get_api_key_wo_tx)],
)
async def deployment_delete(
    project_name: str,
    deployment_number: int,
):
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
        if deployment_number == 0:
            deployment = await get_last_deployment(dbsession, project)
        else:
            deployment = await get_deployment_by_number(
                dbsession, project, deployment_number
            )
        if deployment is None:
            raise HTTPException(status_code=404)
        if deployment.status not in ["QUEUED", "PREPARING"]:
            raise HTTPException(
                422,
                f"Cannot cancel deployment {deployment.number}, status {deployment.status} not one of QUEUED or PREPARING",
            )
        cancel_deployment(deployment)


@router.get(
    "/api/projects/{project_name}/deployments/{deployment_number}/output",
    dependencies=[Depends(get_api_key_wo_tx)],
)
async def deployment_output_get(
    project_name: str,
    deployment_number: int,
    last_event_id: Annotated[str | None, Header()] = None,
):
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
        if deployment_number == 0:
            deployment = await get_last_deployment(dbsession, project)
        else:
            deployment = await get_deployment_by_number(
                dbsession, project, deployment_number
            )
        if deployment is None:
            raise HTTPException(status_code=404)
        source = commandoutputs.deployment_source(deployment.id)
        after = None
        if last_event_id is not None:
            output = await commandoutputs.get_by_id(source, last_event_id)
            if output is not None:
                after = output.created

    async def get_build_output(source: str, after: datetime | None):
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

    return EventSourceResponse(get_build_output(source, after))
