import asyncio
import json
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sse_starlette import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key, get_api_key_sync, get_api_key_wo_tx
from disco.endpoints.dependencies import (
    get_db,
    get_project_from_url,
    get_project_from_url_sync,
)
from disco.models import ApiKey, Project
from disco.models.db import AsyncSession
from disco.utils import commandoutputs
from disco.utils.apikeys import get_valid_api_key_by_id
from disco.utils.deploymentflow import enqueue_deployment
from disco.utils.deployments import (
    cancel_deployment,
    create_deployment,
    get_deployment_by_number,
    get_deployments_with_status,
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
async def deployments_post(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    req_body: DeploymentRequestBody,
    background_tasks: BackgroundTasks,
):
    deployment = await create_deployment(
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
)
async def deployment_delete(
    project_name: str,
    deployment_number: int,
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
):
    async with AsyncSession.begin() as dbsession:
        api_key = await get_valid_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        project = await get_project_by_name(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
        cancelled_deployments = []
        if deployment_number == 0:
            deployments_queued = await get_deployments_with_status(
                dbsession, project, "QUEUED"
            )
            for deployment in deployments_queued:
                await cancel_deployment(deployment, by_api_key=api_key)
                cancelled_deployments.append(deployment.number)
            deployments_preparing = await get_deployments_with_status(
                dbsession, project, "PREPARING"
            )
            for deployment in deployments_preparing:
                await cancel_deployment(deployment, by_api_key=api_key)
                cancelled_deployments.append(deployment.number)
            deployments_replacing = await get_deployments_with_status(
                dbsession, project, "REPLACING"
            )
            for deployment in deployments_replacing:
                await cancel_deployment(deployment, by_api_key=api_key)
                cancelled_deployments.append(deployment.number)
        else:
            single_deployment = await get_deployment_by_number(
                dbsession, project, deployment_number
            )
            if single_deployment is None:
                raise HTTPException(status_code=404)
            if single_deployment.status not in ["QUEUED", "PREPARING", "REPLACING"]:
                raise HTTPException(
                    422,
                    f"Cannot cancel deployment {single_deployment.number}, "
                    f"status {single_deployment.status} not one of QUEUED, PREPARING, REPLACING",
                )
            await cancel_deployment(single_deployment, by_api_key=api_key)
            cancelled_deployments.append(single_deployment.number)
        return {
            "cancelledDeployments": [
                {"number": number} for number in sorted(cancelled_deployments)
            ]
        }


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
