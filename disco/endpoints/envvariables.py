import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, Field
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key
from disco.endpoints.loaders import get_project_from_url
from disco.models import ApiKey, Project, ProjectEnvironmentVariable
from disco.models.db import get_db
from disco.utils.envvariables import (
    delete_env_variable,
    get_env_variable_by_name,
    get_env_variables_for_project,
    set_env_variables,
)

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key)])


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
                "value": env_variable.value,
            }
            for env_variable in env_variables
        ]
    }


# TODO proper validation
class EnvVariable(BaseModel):
    name: str
    value: str


class ReqEnvVariables(BaseModel):
    env_variables: list[EnvVariable] = Field(..., alias="envVariables")


@router.post("/projects/{project_name}/env")
def env_variables_post(
    dbsession: Annotated[DBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    req_env_variables: ReqEnvVariables,
):
    deployment = set_env_variables(
        dbsession=dbsession,
        project=project,
        env_variables=[
            (env_var.name, env_var.value) for env_var in req_env_variables.env_variables
        ],
        by_api_key=api_key,
    )
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
            "value": env_variable.value,
        }
    }


@router.get("/projects/{project_name}/env/{env_var_name}")
def env_variable_delete(
    dbsession: Annotated[DBSession, Depends(get_db)],
    env_variable: Annotated[
        ProjectEnvironmentVariable, Depends(get_env_variable_from_url)
    ],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
):
    deployment = delete_env_variable(
        dbsession=dbsession,
        env_variable=env_variable,
        by_api_key=api_key,
    )
    return {
        "deployment": {
            "number": deployment.number,
        }
        if deployment is not None
        else None,
    }
