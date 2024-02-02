import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key
from disco.endpoints.loaders import get_project_from_url
from disco.models import ApiKey, Project
from disco.models.db import get_db
from disco.utils.deployments import create_deployment

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key)])


# TODO proper validation
class NewProject(BaseModel):
    commit: str | None
    disco_config: str | None = Field(..., alias="discoConfig")


@router.post("/projects/{project_name}/deployments", status_code=201)
def deployments_post(
    dbsession: Annotated[DBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    new_deployment: NewProject,
):
    deployment = create_deployment(
        dbsession=dbsession,
        project=project,
        commit_hash=new_deployment.commit,
        disco_config=new_deployment.disco_config,
        by_api_key=api_key,
    )
    # TODO change code so that we always receive a deployment?
    if deployment is None:
        return {"deployment": None}
    return {
        "deployment": {
            "number": deployment.number,
        },
    }
