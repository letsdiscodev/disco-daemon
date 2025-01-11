import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key, get_api_key_sync
from disco.endpoints.dependencies import (
    get_db,
    get_project_from_url,
    get_project_from_url_sync,
    get_sync_db,
)
from disco.models import ApiKey, Project, ProjectEnvironmentVariable
from disco.utils.deploymentflow import enqueue_deployment
from disco.utils.deployments import maybe_create_deployment
from disco.utils.encryption import decrypt
from disco.utils.envvariables import (
    delete_env_variable,
    get_env_variable_by_name,
    get_env_variables_for_project_sync,
    set_env_variables,
)

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_sync)])


@router.get("/api/projects/{project_name}/env")
def env_variables_get(
    dbsession: Annotated[DBSession, Depends(get_sync_db)],
    project: Annotated[Project, Depends(get_project_from_url_sync)],
):
    env_variables = get_env_variables_for_project_sync(dbsession, project)
    return {
        "envVariables": [
            {
                "name": env_variable.name,
                "value": decrypt(env_variable.value),
            }
            for env_variable in env_variables
        ]
    }


class EnvVariable(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z_]+[a-zA-Z0-9_]*$", max_length=255)
    value: str = Field(..., max_length=4000)


class ReqEnvVariables(BaseModel):
    env_variables: list[EnvVariable] = Field(..., alias="envVariables", min_length=1)


@router.post("/api/projects/{project_name}/env")
async def env_variables_post(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    req_env_variables: ReqEnvVariables,
    background_tasks: BackgroundTasks,
):
    await set_env_variables(
        dbsession=dbsession,
        project=project,
        env_variables=[
            (env_var.name, env_var.value) for env_var in req_env_variables.env_variables
        ],
        by_api_key=api_key,
    )
    deployment = await maybe_create_deployment(
        dbsession=dbsession,
        project=project,
        commit_hash=None,
        disco_file=None,
        by_api_key=api_key,
    )
    if deployment is not None:
        background_tasks.add_task(enqueue_deployment, deployment.id)
    return {
        "deployment": {
            "number": deployment.number,
        }
        if deployment is not None
        else None,
    }


async def get_env_variable_from_url(
    dbsession: Annotated[AsyncDBSession, Depends(get_sync_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    env_var_name: Annotated[str, Path()],
):
    env_variable = await get_env_variable_by_name(
        dbsession=dbsession,
        project=project,
        name=env_var_name,
    )
    if env_variable is None:
        raise HTTPException(status_code=404)
    yield env_variable


@router.get("/api/projects/{project_name}/env/{env_var_name}")
async def env_variable_get(
    env_variable: Annotated[
        ProjectEnvironmentVariable, Depends(get_env_variable_from_url)
    ],
):
    return {
        "envVariable": {
            "name": env_variable.name,
            "value": decrypt(env_variable.value),
        }
    }


@router.delete("/api/projects/{project_name}/env/{env_var_name}")
async def env_variable_delete(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    env_variable: Annotated[
        ProjectEnvironmentVariable, Depends(get_env_variable_from_url)
    ],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    background_tasks: BackgroundTasks,
):
    await delete_env_variable(
        dbsession=dbsession,
        env_variable=env_variable,
    )
    deployment = await maybe_create_deployment(
        dbsession=dbsession,
        project=env_variable.project,
        commit_hash=None,
        disco_file=None,
        by_api_key=api_key,
    )

    if deployment is not None:
        background_tasks.add_task(enqueue_deployment, deployment.id)
    return {
        "deployment": {
            "number": deployment.number,
        }
        if deployment is not None
        else None,
    }
