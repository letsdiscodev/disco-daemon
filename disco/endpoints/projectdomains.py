import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from disco.auth import get_api_key
from disco.endpoints.dependencies import get_db, get_project_from_url
from disco.models import ApiKey, Project, ProjectDomain
from disco.utils import keyvalues
from disco.utils.projectdomains import (
    add_domain,
    get_domain_by_id,
    get_domain_by_name,
    remove_domain,
)

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key)])


@router.get("/api/projects/{project_name}/domains")
async def domains_get(
    project: Annotated[Project, Depends(get_project_from_url)],
):
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
    domain: str = Field(..., pattern=r"^\S+$", max_length=255)


@router.post("/api/projects/{project_name}/domains", status_code=201)
async def domains_post(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    req_body: AddDomainReqBody,
):
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


async def get_domain_from_url(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    domain_id: Annotated[str, Path()],
):
    domain = await get_domain_by_id(
        dbsession=dbsession,
        domain_id=domain_id,
    )
    if domain is None:
        raise HTTPException(status_code=404)
    if domain.project_id != project.id:
        raise HTTPException(status_code=404)
    yield domain


@router.delete("/api/projects/{project_name}/domains/{domain_id}", status_code=204)
async def domain_delete(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    domain: Annotated[ProjectDomain, Depends(get_domain_from_url)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
):
    await remove_domain(
        dbsession=dbsession,
        domain=domain,
        by_api_key=api_key,
    )
    return {}
