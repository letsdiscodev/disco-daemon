import logging
from typing import Annotated

import randomname
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key
from disco.endpoints.dependencies import get_db, get_project_from_url
from disco.models import ApiKey, Project
from disco.utils.deployments import create_deployment
from disco.utils.projects import create_project, delete_project, get_all_projects

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key)])


# TODO proper validation
class NewProjectRequestBody(BaseModel):
    name: str
    github_repo: str | None = Field(None, alias="githubRepo")
    domain: str | None = None
    generate_suffix: bool = Field(False, alias="generateSuffix")
    deploy: bool = False
    commit: str = "_DEPLOY_LATEST_"


@router.post("/projects", status_code=201)
def projects_post(
    dbsession: Annotated[DBSession, Depends(get_db)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    req_body: NewProjectRequestBody,
):
    if req_body.generate_suffix:
        req_body.name = f"{req_body.name}-{randomname.get_name()}"
    project, ssh_key_pub = create_project(
        dbsession=dbsession,
        name=req_body.name,
        github_repo=req_body.github_repo,
        domain=req_body.domain,
        by_api_key=api_key,
    )
    if ssh_key_pub is None and req_body.deploy:
        deployment = create_deployment(
            dbsession=dbsession,
            project=project,
            commit_hash=req_body.commit,
            disco_file=None,
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
