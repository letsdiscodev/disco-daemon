import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError

from disco.auth import get_api_key_wo_tx
from disco.endpoints.dependencies import get_project_name_from_url_wo_tx
from disco.models.db import ReadSession
from disco.utils import docker
from disco.utils.apikeys import get_api_key_by_id
from disco.utils.deployments import get_live_deployment
from disco.utils.discofile import ServiceType, get_disco_file_from_str
from disco.utils.projects import get_project_by_name

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])


@router.get("/api/projects/{project_name}/scale")
async def scale_get(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
):
    async with ReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
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
async def scale_post(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    req_body: ScaleRequestBody,
):
    async with ReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        api_key = await get_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        deployment = await get_live_deployment(dbsession, project)
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
            await docker.scale(internal_name_scale)
