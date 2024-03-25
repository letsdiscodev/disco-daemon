import logging
from typing import Annotated

import randomname
from fastapi import APIRouter, Depends
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key
from disco.endpoints.dependencies import get_db, get_project_from_url
from disco.endpoints.envvariables import EnvVariable
from disco.models import ApiKey, Project
from disco.utils import sshkeys
from disco.utils.deployments import create_deployment, get_live_deployment
from disco.utils.dns import domain_points_to_here
from disco.utils.encryption import decrypt
from disco.utils.envvariables import get_env_variables_for_project
from disco.utils.filesystem import (
    get_caddy_key_crt,
    get_caddy_key_key,
    get_caddy_key_meta,
    set_caddy_key_crt,
    set_caddy_key_key,
    set_caddy_key_meta,
)
from disco.utils.projects import (
    create_project,
    delete_project,
    get_all_projects,
    get_project_by_domain,
    get_project_by_name,
)

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key)])


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
        pattern=r"^((git@github\.com:)|(https://github\.com/))\S+/\S+(\.git)?$",
    )
    github_webhook_token: str | None = Field(
        None,
        alias="githubWebhookToken",
    )
    domain: str | None = None
    ssh: Ssh | None = None
    env_variables: list[EnvVariable] = Field([], alias="envVariables")
    caddy: CaddyKey | None = None
    generate_suffix: bool = Field(False, alias="generateSuffix")
    deploy: bool = False
    commit: str = "_DEPLOY_LATEST_"
    deployment_number: int | None = Field(None, alias="deploymentNumber")


@router.post("/projects", status_code=201)
def projects_post(
    dbsession: Annotated[DBSession, Depends(get_db)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    req_body: NewProjectRequestBody,
):
    if req_body.generate_suffix:
        req_body.name = f"{req_body.name}-{randomname.get_name()}"
    project = get_project_by_name(dbsession, req_body.name)
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
        project = get_project_by_domain(dbsession, req_body.domain)
        if project is not None:
            raise RequestValidationError(
                errors=(
                    ValidationError.from_exception_data(
                        "ValueError",
                        [
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "value_error",
                                    "Domain already taken by other project",
                                ),
                                loc=("body", "domain"),
                                input=req_body.domain,
                            )
                        ],
                    )
                ).errors()
            )
        if req_body.caddy is None and not domain_points_to_here(
            dbsession, req_body.domain
        ):
            raise RequestValidationError(
                errors=(
                    ValidationError.from_exception_data(
                        "ValueError",
                        [
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "value_error",
                                    "Domain does not point to server IP address",
                                ),
                                loc=("body", "domain"),
                                input=req_body.domain,
                            )
                        ],
                    )
                ).errors()
            )
    if req_body.caddy is not None and req_body.domain is not None:
        # TODO validation (raise exception if domain not set and caddy is set)
        set_caddy_key_crt(req_body.domain, req_body.caddy.crt)
        set_caddy_key_key(req_body.domain, req_body.caddy.key)
        set_caddy_key_meta(req_body.domain, req_body.caddy.meta)
    github_repo = req_body.github_repo
    if github_repo is not None and not github_repo.endswith(".git"):
        github_repo += ".git"
    project, ssh_key_pub = create_project(
        dbsession=dbsession,
        name=req_body.name,
        github_repo=github_repo,
        github_webhook_token=req_body.github_webhook_token,
        domain=req_body.domain,
        ssh_key_pub=req_body.ssh.public_key if req_body.ssh is not None else None,
        ssh_key_private=req_body.ssh.private_key if req_body.ssh is not None else None,
        env_variables=[
            (env_var.name, env_var.value) for env_var in req_body.env_variables
        ],
        by_api_key=api_key,
    )
    if req_body.deploy:
        deployment = create_deployment(
            dbsession=dbsession,
            project=project,
            commit_hash=req_body.commit,
            disco_file=None,
            number=req_body.deployment_number,
            by_api_key=api_key,
        )
    else:
        deployment = None
    return {
        "project": {
            "name": project.name,
            "githubRepo": project.github_repo,
            "domain": project.domain,
            "githubWebhookToken": project.github_webhook_token,
        },
        "sshKeyPub": ssh_key_pub,
        "deployment": {
            "number": deployment.number,
        }
        if deployment is not None
        else None,
    }


@router.get("/projects")
def projects_get(dbsession: Annotated[DBSession, Depends(get_db)]):
    projects = get_all_projects(dbsession)
    return {
        "projects": [
            {
                "name": project.name,
                "githubRepo": project.github_repo,
                "domain": project.domain,
                "githubWebhookToken": project.github_webhook_token,
            }
            for project in projects
        ],
    }


@router.delete("/projects/{project_name}", status_code=200)
def projects_delete(
    dbsession: Annotated[DBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
):
    delete_project(dbsession, project, api_key)
    return {"deleted": True}


@router.get("/projects/{project_name}/export")
def export_get(
    dbsession: Annotated[DBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
):
    log.info("Exporting project %s by %s", project.log(), api_key.log())
    env_variables = get_env_variables_for_project(dbsession, project)
    deployment = get_live_deployment(dbsession, project)
    return {
        "name": project.name,
        "domain": project.domain,
        "githubRepo": project.github_repo,
        "githubWebhookToken": project.github_webhook_token,
        "ssh": {
            "privateKey": sshkeys.get_key_private(project.name),
            "publicKey": sshkeys.get_key_pub(project.name),
        }
        if sshkeys.get_key_pub(project.name) is not None
        else None,
        "envVariables": [
            {
                "name": env_variable.name,
                "value": decrypt(env_variable.value),
            }
            for env_variable in env_variables
        ],
        "caddy": {
            "crt": get_caddy_key_crt(project.domain),
            "key": get_caddy_key_key(project.domain),
            "meta": get_caddy_key_meta(project.domain),
        }
        if project.domain is not None
        else None,
        "deployment": {
            "number": deployment.number,
            "commit": deployment.commit_hash,
        }
        if deployment is not None
        else None,
    }
