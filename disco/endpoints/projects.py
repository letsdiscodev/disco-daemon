import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key, get_api_key_sync
from disco.endpoints.dependencies import (
    get_db,
    get_db_sync,
    get_project_from_url,
    get_project_from_url_sync,
)
from disco.endpoints.envvariables import EnvVariable
from disco.models import ApiKey, Project
from disco.utils import keyvalues
from disco.utils.deploymentflow import enqueue_deployment
from disco.utils.deployments import (
    create_deployment,
    get_live_deployment_sync,
)
from disco.utils.discofile import get_disco_file_from_str
from disco.utils.encryption import decrypt
from disco.utils.envvariables import (
    get_env_variables_for_project_sync,
    set_env_variables,
)
from disco.utils.filesystem import (
    get_caddy_key_crt,
    get_caddy_key_key,
    get_caddy_key_meta,
)
from disco.utils.github import get_all_repos, repo_is_public
from disco.utils.projectdomains import DOMAIN_REGEX, add_domain
from disco.utils.projects import (
    create_project,
    delete_project,
    get_all_projects_sync,
    get_project_by_domain,
    get_project_by_name,
    set_project_github_repo,
)
from disco.utils.randomname import generate_random_name

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_sync)])


class Ssh(BaseModel):
    public_key: str = Field(..., alias="publicKey")
    private_key: str = Field(..., alias="privateKey")


class CaddyKey(BaseModel):
    crt: str
    key: str
    meta: str


class NewProjectRequestBody(BaseModel):
    name: str = Field(..., pattern=r"^[a-z][a-z0-9\-]*$", max_length=255)
    github_repo: str | None = Field(
        None,
        alias="githubRepo",
        pattern=r"^\S+/\S+$",
    )
    branch: str | None = None
    domain: str | None = Field(None, pattern=DOMAIN_REGEX)
    env_variables: list[EnvVariable] = Field([], alias="envVariables")
    caddy: CaddyKey | None = None
    generate_suffix: bool = Field(False, alias="generateSuffix")
    commit: str = "_DEPLOY_LATEST_"
    deployment_number: int | None = Field(None, alias="deploymentNumber")


async def validate_create_project(
    dbsession: AsyncDBSession, req_body: NewProjectRequestBody
) -> None:
    project = await get_project_by_name(dbsession, req_body.name)
    if project is not None:
        raise RequestValidationError(
            errors=(
                ValidationError.from_exception_data(
                    "ValueError",
                    [
                        InitErrorDetails(
                            type=PydanticCustomError(
                                "value_error", "Project name already exists"
                            ),
                            loc=("body", "name"),
                            input=req_body.name,
                        )
                    ],
                )
            ).errors()
        )
    if req_body.domain is not None:
        project = await get_project_by_domain(dbsession, req_body.domain)
        if project is not None:
            raise RequestValidationError(
                errors=(
                    ValidationError.from_exception_data(
                        "ValueError",
                        [
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "value_error",
                                    "Domain already taken by a project",
                                ),
                                loc=("body", "domain"),
                                input=req_body.domain,
                            )
                        ],
                    )
                ).errors()
            )
        disco_host = await keyvalues.get_value_str(dbsession, "DISCO_HOST")
        if req_body.domain == disco_host:
            raise RequestValidationError(
                errors=(
                    ValidationError.from_exception_data(
                        "ValueError",
                        [
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "value_error",
                                    "Domain already taken by Disco",
                                ),
                                loc=("body", "domain"),
                                input=req_body.domain,
                            )
                        ],
                    )
                ).errors()
            )
    if req_body.github_repo is not None:
        repos = await get_all_repos(dbsession)
        if req_body.github_repo not in [
            repo.full_name for repo in repos
        ] and not await repo_is_public(req_body.github_repo):
            raise RequestValidationError(
                errors=(
                    ValidationError.from_exception_data(
                        "ValueError",
                        [
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "value_error",
                                    "You need to give permissions to this repo first",
                                ),
                                loc=("body", "githubRepo"),
                                input=req_body.github_repo,
                            )
                        ],
                    )
                ).errors()
            )


@router.post("/api/projects", status_code=201)
async def projects_post(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    req_body: NewProjectRequestBody,
    background_tasks: BackgroundTasks,
):
    if req_body.generate_suffix:
        suffix = await generate_random_name()
        req_body.name = f"{req_body.name}-{suffix}"
    await validate_create_project(dbsession=dbsession, req_body=req_body)
    if req_body.caddy is not None and req_body.domain is not None:
        # TODO rewrite with await
        pass
        # # TODO validation (raise exception if domain not set and caddy is set)
        # set_caddy_key_crt(req_body.domain, req_body.caddy.crt)
        # set_caddy_key_key(req_body.domain, req_body.caddy.key)
        # set_caddy_key_meta(req_body.domain, req_body.caddy.meta)
    project = create_project(
        dbsession=dbsession,
        name=req_body.name,
        by_api_key=api_key,
    )
    if req_body.github_repo is not None:
        await set_project_github_repo(
            dbsession=dbsession,
            project=project,
            github_repo=req_body.github_repo,
            branch=req_body.branch,
            by_api_key=api_key,
        )
    await set_env_variables(
        dbsession=dbsession,
        project=project,
        env_variables=[
            (env_var.name, env_var.value) for env_var in req_body.env_variables
        ],
        by_api_key=api_key,
    )
    if req_body.domain is not None:
        await add_domain(
            dbsession=dbsession,
            project=project,
            domain_name=req_body.domain,
            by_api_key=api_key,
        )

    if req_body.github_repo is not None:
        deployment = await create_deployment(
            dbsession=dbsession,
            project=project,
            commit_hash=req_body.commit,
            disco_file=None,
            number=req_body.deployment_number,
            by_api_key=api_key,
        )
        background_tasks.add_task(enqueue_deployment, deployment.id)
    else:
        deployment = None
    return {
        "project": {
            "name": project.name,
        },
        "deployment": {
            "number": deployment.number,
        }
        if deployment is not None
        else None,
    }


@router.get("/api/projects")
def projects_get(dbsession: Annotated[DBSession, Depends(get_db_sync)]):
    projects = get_all_projects_sync(dbsession)
    return {
        "projects": [
            {
                "name": project.name,
                "github": {
                    "fullName": project.github_repo.full_name,
                    "branch": project.github_repo.branch,
                }
                if project.github_repo is not None
                else None,
            }
            for project in projects
        ],
    }


@router.delete("/api/projects/{project_name}", status_code=200)
async def projects_delete(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
):
    await delete_project(dbsession, project, api_key)
    return {"deleted": True}


@router.get("/api/projects/{project_name}/export")
def export_get(
    dbsession: Annotated[DBSession, Depends(get_db_sync)],
    project: Annotated[Project, Depends(get_project_from_url_sync)],
    api_key: Annotated[ApiKey, Depends(get_api_key_sync)],
):
    log.info("Exporting project %s by %s", project.log(), api_key.log())
    env_variables = get_env_variables_for_project_sync(dbsession, project)
    deployment = get_live_deployment_sync(dbsession, project)
    volume_names = []
    if deployment is not None:
        disco_file = get_disco_file_from_str(deployment.disco_file)
        for service in disco_file.services.values():
            for volume in service.volumes:
                volume_names.append(volume.name)
    return {
        "name": project.name,
        "domains": [domain.name for domain in project.domains],
        "envVariables": [
            {
                "name": env_variable.name,
                "value": decrypt(env_variable.value),
            }
            for env_variable in env_variables
        ],
        "caddy": [
            {
                "name": domain.name,
                "crt": get_caddy_key_crt(domain.name),
                "key": get_caddy_key_key(domain.name),
                "meta": get_caddy_key_meta(domain.name),
            }
            for domain in project.domains
        ],
        "deployment": {
            "number": deployment.number,
            "commit": deployment.commit_hash,
        }
        if deployment is not None
        else None,
        "volumes": volume_names,
    }
