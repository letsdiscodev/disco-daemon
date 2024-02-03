import asyncio
import json
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm.session import Session as DBSession
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key, get_api_key_wo_tx
from disco.endpoints.dependencies import get_db, get_project_from_url
from disco.models import ApiKey, Project
from disco.models.db import Session
from disco.utils import commandoutputs
from disco.utils.deployments import create_deployment, get_deployment_by_number
from disco.utils.projects import get_project_by_name

log = logging.getLogger(__name__)

router = APIRouter()


# TODO proper validation
class NewProject(BaseModel):
    commit: str | None
    disco_config: str | None = Field(..., alias="discoConfig")


@router.post(
    "/projects/{project_name}/deployments",
    status_code=201,
    dependencies=[Depends(get_api_key)],
)
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


@router.get(
    "/projects/{project_name}/deployments/{deployment_number}/output",
    dependencies=[Depends(get_api_key_wo_tx)],
)
async def deployment_output_get(
    project_name: str, deployment_number: int, after: datetime | None = None
):
    with Session() as dbsession:
        with dbsession.begin():
            project = get_project_by_name(dbsession, project_name)
            if project is None:
                raise HTTPException(status_code=404)
            deployment = get_deployment_by_number(dbsession, project, deployment_number)
            if deployment is None:
                raise HTTPException(status_code=404)
            source = f"DEPLOYMENT_{deployment.id}"

    async def get_build_output(source: str, after: datetime | None):
        while True:
            with Session() as dbsession:
                with dbsession.begin():
                    output = commandoutputs.get_next(dbsession, source, after=after)
                    log.info("One row: %s", str(output))
                    if output is not None:
                        if output.text is None:
                            return
                        after = output.created
                        yield json.dumps(
                            {
                                "timestamp": output.created.isoformat(),
                                "text": output.text,
                            }
                        )
            if output is None:
                await asyncio.sleep(0.1)

    return EventSourceResponse(get_build_output(source, after))
