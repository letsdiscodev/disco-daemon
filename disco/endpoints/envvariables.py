import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key
from disco.endpoints.dependencies import get_db, get_project_from_url
from disco.models import ApiKey, Project, ProjectEnvironmentVariable
from disco.utils.encryption import decrypt
from disco.utils.envvariables import (
    delete_env_variable,
    get_env_variable_by_name,
    get_env_variables_for_project,
    set_env_variables,
)
from disco.utils.mq.tasks import enqueue_task_deprecated

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key)])


def process_deployment(deployment_id: str) -> None:
    enqueue_task_deprecated(
        task_name="PROCESS_DEPLOYMENT",
        body=dict(
            deployment_id=deployment_id,
        ),
    )


@router.get("/projects/{project_name}/env")
def env_variables_get(
    dbsession: Annotated[DBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
):
    env_variables = get_env_variables_for_project(dbsession, project)
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


@router.post("/projects/{project_name}/env")
def env_variables_post(
    dbsession: Annotated[DBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    req_env_variables: ReqEnvVariables,
    background_tasks: BackgroundTasks,
):
    deployment = set_env_variables(
        dbsession=dbsession,
        project=project,
        env_variables=[
            (env_var.name, env_var.value) for env_var in req_env_variables.env_variables
        ],
        by_api_key=api_key,
    )
    if deployment is not None:
        background_tasks.add_task(process_deployment, deployment.id)
    return {
        "deployment": {
            "number": deployment.number,
        }
        if deployment is not None
        else None,
    }


def get_env_variable_from_url(
    dbsession: Annotated[DBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    env_var_name: Annotated[str, Path()],
):
    env_variable = get_env_variable_by_name(
        dbsession=dbsession,
        project=project,
        name=env_var_name,
    )
    if env_variable is None:
        raise HTTPException(status_code=404)
    yield env_variable


@router.get("/projects/{project_name}/env/{env_var_name}")
def env_variable_get(
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


@router.delete("/projects/{project_name}/env/{env_var_name}")
def env_variable_delete(
    dbsession: Annotated[DBSession, Depends(get_db)],
    env_variable: Annotated[
        ProjectEnvironmentVariable, Depends(get_env_variable_from_url)
    ],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    background_tasks: BackgroundTasks,
):
    deployment = delete_env_variable(
        dbsession=dbsession,
        env_variable=env_variable,
        by_api_key=api_key,
    )
    if deployment is not None:
        background_tasks.add_task(process_deployment, deployment.id)
    return {
        "deployment": {
            "number": deployment.number,
        }
        if deployment is not None
        else None,
    }
