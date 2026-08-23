import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path
from pydantic import BaseModel, Field, model_validator

from disco.auth import get_api_key_wo_tx
from disco.endpoints.dependencies import get_project_name_from_url_wo_tx
from disco.models.db import AsyncReadSession, AsyncSession
from disco.utils.apikeys import get_api_key_by_id
from disco.utils.deploymentflow import enqueue_deployment
from disco.utils.deployments import maybe_create_deployment
from disco.utils.encryption import decrypt
from disco.utils.envvariables import (
    delete_env_variable,
    delete_env_variables_by_name,
    get_env_variable_by_name,
    get_env_variables_for_project,
    set_env_variables,
)
from disco.utils.projects import get_project_by_name

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])


@router.get("/api/projects/{project_name}/env")
async def env_variables_get(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
):
    async with AsyncReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        env_variables = await get_env_variables_for_project(dbsession, project)
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


class EnvVariableMutation(BaseModel):
    name: str = Field(..., pattern=r"^[a-zA-Z_]+[a-zA-Z0-9_]*$", max_length=255)
    value: str | None = Field(..., max_length=4000)


class ReqEnvVariableMutations(BaseModel):
    env_variables: list[EnvVariableMutation] = Field(
        ..., alias="envVariables", min_length=1
    )

    @model_validator(mode="after")
    def no_duplicate_names(self) -> "ReqEnvVariableMutations":
        seen: set[str] = set()
        for ev in self.env_variables:
            if ev.name in seen:
                raise ValueError(f"Duplicate env variable name: {ev.name}")
            seen.add(ev.name)
        return self


@router.post("/api/projects/{project_name}/env")
async def env_variables_post(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    req_env_variables: ReqEnvVariableMutations,
    background_tasks: BackgroundTasks,
):
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        api_key = await get_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        sets: list[tuple[str, str]] = []
        deletes: list[str] = []
        for ev in req_env_variables.env_variables:
            if ev.value is None:
                deletes.append(ev.name)
            else:
                sets.append((ev.name, ev.value))

        if sets:
            await set_env_variables(
                dbsession=dbsession,
                project=project,
                env_variables=sets,
                by_api_key=api_key,
            )
        actually_deleted = 0
        if deletes:
            actually_deleted = await delete_env_variables_by_name(
                dbsession=dbsession,
                project=project,
                names=deletes,
            )

        deployment = None
        if sets or actually_deleted > 0:
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


async def get_env_var_name_from_url(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    env_var_name: Annotated[str, Path()],
):
    async with AsyncReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        env_variable = await get_env_variable_by_name(
            dbsession=dbsession,
            project=project,
            name=env_var_name,
        )
        if env_variable is None:
            raise HTTPException(status_code=404)
    yield env_var_name


@router.get("/api/projects/{project_name}/env/{env_var_name}")
async def env_variable_get(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    env_var_name: Annotated[str, Depends(get_env_var_name_from_url)],
):
    async with AsyncReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        env_variable = await get_env_variable_by_name(
            dbsession=dbsession,
            project=project,
            name=env_var_name,
        )
        assert env_variable is not None
        return {
            "envVariable": {
                "name": env_variable.name,
                "value": decrypt(env_variable.value),
            }
        }


@router.delete("/api/projects/{project_name}/env/{env_var_name}")
async def env_variable_delete(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    env_var_name: Annotated[str, Depends(get_env_var_name_from_url)],
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    background_tasks: BackgroundTasks,
):
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        api_key = await get_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        env_variable = await get_env_variable_by_name(
            dbsession=dbsession,
            project=project,
            name=env_var_name,
        )
        assert env_variable is not None
        await delete_env_variable(
            dbsession=dbsession,
            env_variable=env_variable,
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
