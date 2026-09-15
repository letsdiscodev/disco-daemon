import asyncio
import json
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key_wo_tx
from disco.endpoints.dependencies import get_project_name_from_url_wo_tx
from disco.models.db import ReadSession, Session
from disco.utils import commandoutputs, pendingfiles
from disco.utils.apikeys import get_api_key_by_id, get_valid_api_key_by_id
from disco.utils.deploymentflow import enqueue_deployment, process_deployment_if_any
from disco.utils.deployments import (
    cancel_deployment,
    create_deployment,
    get_deployment_by_number,
    get_deployments_with_status,
    get_last_deployment,
)
from disco.utils.discofile import DiscoFile
from disco.utils.envvariables import get_env_variable_by_name
from disco.utils.filesystem import rmtree
from disco.utils.projects import get_project_by_name

log = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/api/projects/{project_name}/deployments",
    dependencies=[Depends(get_api_key_wo_tx)],
)
async def deployments_get(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
):
    async with ReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        deployments = await project.awaitable_attrs.deployments
        return {
            "deployments": [
                {
                    "number": deployment.number,
                    "created": deployment.created.isoformat(),
                    "status": deployment.status,
                    "commitHash": deployment.commit_hash,
                    "type": deployment.deployment_type,
                }
                for deployment in deployments
            ]
        }


class DeploymentRequestBody(BaseModel):
    commit: str = Field("_DEPLOY_LATEST_", pattern=r"^\S+$")
    disco_file: DiscoFile | None = Field(None, alias="discoFile")


@router.post(
    "/api/projects/{project_name}/deployments",
    status_code=201,
    dependencies=[Depends(get_api_key_wo_tx)],
)
async def deployments_post(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    req_body: DeploymentRequestBody,
    background_tasks: BackgroundTasks,
):
    if req_body.disco_file is not None:
        await _require_no_github_repo(project_name)
        await _require_no_disco_json_path(project_name)
        received_path = await pendingfiles.write_disco_file(
            project_name, req_body.disco_file
        )
        return await _create_files_deployment(
            project_name, received_path, api_key_id, background_tasks
        )
    async with Session.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        if await project.awaitable_attrs.github_repo is None:
            raise HTTPException(
                status_code=422,
                detail="Project has no GitHub repository to deploy from",
            )
        api_key = await get_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        deployment = await create_deployment(
            dbsession=dbsession,
            project=project,
            deployment_type="GITHUB",
            commit_hash=req_body.commit,
            disco_file=None,
            by_api_key=api_key,
        )
        background_tasks.add_task(enqueue_deployment, deployment.id)
        return {
            "deployment": {
                "number": deployment.number,
            },
        }


async def _require_no_github_repo(project_name: str) -> None:
    """A project is deployed either from its GitHub repository or from files."""
    async with ReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        if await project.awaitable_attrs.github_repo is not None:
            raise HTTPException(
                status_code=422,
                detail="Project has a GitHub repository: deploy a commit, "
                "or remove the repository from the project to deploy files",
            )


async def _require_no_disco_json_path(project_name: str) -> None:
    async with ReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        if await get_env_variable_by_name(dbsession, project, "DISCO_JSON_PATH"):
            raise HTTPException(
                status_code=422,
                detail="Can't deploy discoFile when DISCO_JSON_PATH is set",
            )


async def _create_files_deployment(
    project_name: str,
    received_path: str,
    api_key_id: str,
    background_tasks: BackgroundTasks,
) -> dict:
    async with Session.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        api_key = await get_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        deployment = await create_deployment(
            dbsession=dbsession,
            project=project,
            deployment_type="FILES",
            commit_hash=None,
            disco_file=None,
            by_api_key=api_key,
        )
        await pendingfiles.set_pending(project_name, received_path, deployment.number)
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
    async with Session.begin() as dbsession:
        api_key = await get_valid_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        project = await get_project_by_name(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
        project_id = project.id
        cancelled_deployments = []
        start_next_deployment = False
        if deployment_number == 0:
            deployments_queued = await get_deployments_with_status(
                dbsession, project, "QUEUED"
            )
            for deployment in deployments_queued:
                start_next = await cancel_deployment(deployment, by_api_key=api_key)
                if start_next:
                    start_next_deployment = True
                cancelled_deployments.append(deployment.number)
            deployments_preparing = await get_deployments_with_status(
                dbsession, project, "PREPARING"
            )
            for deployment in deployments_preparing:
                start_next = await cancel_deployment(deployment, by_api_key=api_key)
                if start_next:
                    start_next_deployment = True
                cancelled_deployments.append(deployment.number)
            deployments_replacing = await get_deployments_with_status(
                dbsession, project, "REPLACING"
            )
            for deployment in deployments_replacing:
                start_next = await cancel_deployment(deployment, by_api_key=api_key)
                if start_next:
                    start_next_deployment = True
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
            start_next = await cancel_deployment(single_deployment, by_api_key=api_key)
            if start_next:
                start_next_deployment = True
            cancelled_deployments.append(single_deployment.number)
        if start_next_deployment:
            await process_deployment_if_any(project_id)
        return {
            "cancelledDeployments": [
                {"number": number} for number in sorted(cancelled_deployments)
            ]
        }


@router.post(
    "/api/projects/{project_name}/files",
    status_code=201,
    dependencies=[Depends(get_api_key_wo_tx)],
)
async def files_post(
    request: Request,
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    background_tasks: BackgroundTasks,
):
    """Deploy the project from an uploaded gzipped tar of its files."""
    try:
        received_path = await pendingfiles.receive_tar_gz(
            project_name, request.stream()
        )
    except pendingfiles.FilesArchiveError as ex:
        raise HTTPException(status_code=422, detail=str(ex))
    try:
        await _require_no_github_repo(project_name)
    except HTTPException:
        await rmtree(received_path)
        raise
    return await _create_files_deployment(
        project_name, received_path, api_key_id, background_tasks
    )


@router.get(
    "/api/projects/{project_name}/deployments/{deployment_number}/output",
    dependencies=[Depends(get_api_key_wo_tx)],
)
async def deployment_output_get(
    project_name: str,
    deployment_number: int,
    last_event_id: Annotated[str | None, Header()] = None,
):
    async with ReadSession.begin() as dbsession:
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
