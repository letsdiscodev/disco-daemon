import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key
from disco.endpoints.dependencies import get_db, get_project_from_url
from disco.models import ApiKey, Project
from disco.utils.projects import create_project, delete_project, get_all_projects

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key)])


# TODO proper validation
class NewProject(BaseModel):
    name: str
    github_repo: str | None = Field(..., alias="githubRepo")
    domain: str | None


@router.post("/projects", status_code=201)
def projects_post(
    dbsession: Annotated[DBSession, Depends(get_db)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    new_project: NewProject,
):
    project, ssh_key_pub = create_project(
        dbsession=dbsession,
        name=new_project.name,
        github_repo=new_project.github_repo,
        domain=new_project.domain,
        by_api_key=api_key,
    )
    return {
        "project": {
            "name": project.name,
            "githubRepo": project.github_repo,
            "domain": project.domain,
            "githubWebhookToken": project.github_webhook_token,
        },
        "sshKeyPub": ssh_key_pub,
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
