import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError

from disco.auth import get_api_key_wo_tx
from disco.endpoints.dependencies import get_project_name_from_url_wo_tx
from disco.models.db import AsyncSession
from disco.utils import keyvalues
from disco.utils.apikeys import get_api_key_by_id
from disco.utils.projectdomains import (
    DOMAIN_REGEX,
    add_domain,
    get_domain_by_id,
    get_domain_by_name,
    remove_domain,
)
from disco.utils.projects import get_project_by_name

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])


@router.get("/api/projects/{project_name}/domains")
async def domains_get(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
):
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        domains = await project.awaitable_attrs.domains
        return {
            "domains": [
                {
                    "id": domain.id,
                    "name": domain.name,
                }
                for domain in domains
            ]
        }


class AddDomainReqBody(BaseModel):
    domain: str = Field(..., pattern=DOMAIN_REGEX)


@router.post("/api/projects/{project_name}/domains", status_code=201)
async def domains_post(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    req_body: AddDomainReqBody,
):
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        api_key = await get_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        domain = await get_domain_by_name(dbsession, req_body.domain)
        if domain is not None:
            raise RequestValidationError(
                errors=(
                    ValidationError.from_exception_data(
                        "ValueError",
                        [
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "value_error", "Domain already taken by a project"
                                ),
                                loc=("body", "domain"),
                                input=req_body.domain,
                            )
                        ],
                    )
                ).errors()
            )
        disco_host = await keyvalues.get_value_str(dbsession, "DISCO_HOST")
        assert disco_host is not None
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
        await add_domain(
            dbsession=dbsession,
            project=project,
            domain_name=req_body.domain,
            by_api_key=api_key,
        )
        return {}


async def get_domain_id_from_url(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    domain_id: Annotated[str, Path()],
):
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        domain = await get_domain_by_id(
            dbsession=dbsession,
            domain_id=domain_id,
        )
        if domain is None:
            raise HTTPException(status_code=404)
        if domain.project_id != project.id:
            raise HTTPException(status_code=404)
    yield domain_id


@router.delete("/api/projects/{project_name}/domains/{domain_id}", status_code=204)
async def domain_delete(
    domain_id: Annotated[str, Depends(get_domain_id_from_url)],
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
):
    async with AsyncSession.begin() as dbsession:
        domain = await get_domain_by_id(
            dbsession=dbsession,
            domain_id=domain_id,
        )
        assert domain is not None
        api_key = await get_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        await remove_domain(
            dbsession=dbsession,
            domain=domain,
            by_api_key=api_key,
        )
    return {}
