import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError

from disco.auth import get_api_key_wo_tx
from disco.endpoints.dependencies import get_project_name_from_url_wo_tx
from disco.models.db import AsyncReadSession, AsyncSession
from disco.utils.apikeys import get_api_key_by_id
from disco.utils.encryption import decrypt
from disco.utils.projectkeyvalues import (
    delete_value,
    get_all_key_values_for_project,
    get_value,
    set_value,
)
from disco.utils.projects import get_project_by_name

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key_wo_tx)])


@router.get("/api/projects/{project_name}/keyvalues")
async def key_values_get(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
):
    async with AsyncReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        key_values = await get_all_key_values_for_project(dbsession, project)
        return {
            "keyValues": dict(
                [(key_value.key, decrypt(key_value.value)) for key_value in key_values]
            )
        }


async def get_key_from_url(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    key: Annotated[str, Path(max_length=255)],
):
    async with AsyncReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        value = await get_value(
            dbsession=dbsession,
            project=project,
            key=key,
        )
        if value is None:
            raise HTTPException(status_code=404)
    yield key


@router.get("/api/projects/{project_name}/keyvalues/{key}")
async def key_value_get(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    key: Annotated[str, Depends(get_key_from_url)],
):
    async with AsyncReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        value = await get_value(
            dbsession=dbsession,
            project=project,
            key=key,
        )
        assert value is not None
        return {
            "value": value,
        }


class SetKeyValueRequestBody(BaseModel):
    value: str = Field(..., max_length=2097152)
    previous_value: str | None = Field(None, alias="previousValue", max_length=2097152)


@router.put("/api/projects/{project_name}/keyvalues/{key}")
async def key_value_put(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    key: Annotated[str, Path(max_length=255)],
    req_body: SetKeyValueRequestBody,
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
):
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        api_key = await get_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        prev_value = await get_value(dbsession=dbsession, project=project, key=key)
        if "previous_value" in req_body.model_fields_set:
            if req_body.previous_value != prev_value:
                raise RequestValidationError(
                    errors=(
                        ValidationError.from_exception_data(
                            "ValueError",
                            [
                                InitErrorDetails(
                                    type=PydanticCustomError(
                                        "value_error", "Previous value mismatch"
                                    ),
                                    loc=("body", "previousValue"),
                                    input=req_body.previous_value,
                                )
                            ],
                        )
                    ).errors()
                )
        await set_value(
            dbsession=dbsession,
            project=project,
            key=key,
            value=req_body.value,
            by_api_key=api_key,
        )
        return {"value": req_body.value}


@router.delete("/api/projects/{project_name}/keyvalues/{key}")
async def key_value_delete(
    project_name: Annotated[str, Depends(get_project_name_from_url_wo_tx)],
    key: Annotated[str, Path(max_length=255)],
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
):
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        assert project is not None
        api_key = await get_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        await delete_value(
            dbsession=dbsession,
            project=project,
            key=key,
            by_api_key=api_key,
        )
    return {"deleted": True}
