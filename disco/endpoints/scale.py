import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key_sync
from disco.endpoints.dependencies import (
    get_db,
    get_db_sync,
    get_project_from_url,
    get_project_from_url_sync,
)
from disco.models import ApiKey, Project
from disco.utils import docker
from disco.utils.deployments import get_live_deployment, get_live_deployment_sync
from disco.utils.discofile import ServiceType, get_disco_file_from_str

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_sync)])


@router.get("/api/projects/{project_name}/scale")
async def scale_get(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
):
    deployment = await get_live_deployment(dbsession, project)
    if deployment is None:
        services = []
    else:
        services = await docker.list_services_for_deployment(
            project.name, deployment.number
        )
    return {
        "services": [
            {
                "name": service.name,
                "scale": service.replicas,
            }
            for service in services
        ]
    }


class ScaleRequestBody(BaseModel):
    services: dict[str, int]


@router.post("/api/projects/{project_name}/scale")
def scale_post(
    dbsession: Annotated[DBSession, Depends(get_db_sync)],
    project: Annotated[Project, Depends(get_project_from_url_sync)],
    api_key: Annotated[ApiKey, Depends(get_api_key_sync)],
    req_body: ScaleRequestBody,
):
    deployment = get_live_deployment_sync(dbsession, project)
    if deployment is None:
        services = set()
    else:
        disco_file = get_disco_file_from_str(deployment.disco_file)
        services = set(
            [
                service
                for service in disco_file.services
                if disco_file.services[service].type == ServiceType.container
            ]
        )
    invalid_services = []
    for service in req_body.services:
        if service not in services:
            invalid_services.append(service)
    if len(invalid_services) > 0:
        raise RequestValidationError(
            errors=(
                ValidationError.from_exception_data(
                    "ValueError",
                    [
                        InitErrorDetails(
                            type=PydanticCustomError(
                                "value_error",
                                "Service name not in current deployment",
                            ),
                            loc=("body", "services"),
                            input=service,
                        )
                        for service in invalid_services
                    ],
                )
            ).errors()
        )
    if len(req_body.services) > 0:
        assert deployment is not None
        log.info(
            "Scaling services for project %s %s by %s",
            project.log(),
            " ".join([f"{s}={n}" for s, n in req_body.services.items()]),
            api_key.log(),
        )
        internal_name_scale = dict(
            (
                docker.service_name(
                    deployment.project_name, service, deployment.number
                ),
                scale,
            )
            for service, scale in req_body.services.items()
        )
        asyncio.run(docker.scale(internal_name_scale))
