import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from sqlalchemy.orm.session import Session as DBSession

from disco.auth import get_api_key
from disco.endpoints.dependencies import get_db, get_project_from_url
from disco.models import ApiKey, Project
from disco.utils.projectkeyvalues import (
    delete_value,
    get_all_key_values_for_project,
    get_value,
    set_value,
)

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_api_key)])


@router.get("/projects/{project_name}/keyvalues")
def key_values_get(
    dbsession: Annotated[DBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
):
    key_values = get_all_key_values_for_project(dbsession, project)
    return {
        "keyValues": dict(
            [(key_value.key, key_value.value) for key_value in key_values]
        )
    }


def get_value_from_key_in_url(
    dbsession: Annotated[DBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    key: Annotated[str, Path(max_length=255)],
):
    value = get_value(
        dbsession=dbsession,
        project=project,
        key=key,
    )
    if value is None:
        raise HTTPException(status_code=404)
    yield value


@router.get("/projects/{project_name}/keyvalues/{key}")
def key_value_get(
    value: Annotated[str, Depends(get_value_from_key_in_url)],
):
    return {
        "value": value,
    }


class SetKeyValueRequestBody(BaseModel):
    value: str = Field(..., max_length=2097152)
    previous_value: str | None = Field(None, alias="previousValue", max_length=2097152)


@router.put("/projects/{project_name}/keyvalues/{key}")
def key_value_put(
    dbsession: Annotated[DBSession, Depends(get_db)],
    key: Annotated[str, Path(max_length=255)],
    req_body: SetKeyValueRequestBody,
    project: Annotated[Project, Depends(get_project_from_url)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
):
    prev_value = get_value(dbsession=dbsession, project=project, key=key)
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
    set_value(
        dbsession=dbsession,
        project=project,
        key=key,
        value=req_body.value,
        by_api_key=api_key,
    )
    return {"value": req_body.value}


@router.delete("/projects/{project_name}/keyvalues/{key}")
def key_value_delete(
    dbsession: Annotated[DBSession, Depends(get_db)],
    project: Annotated[Project, Depends(get_project_from_url)],
    key: Annotated[str, Path(max_length=255)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
):
    delete_value(
        dbsession=dbsession,
        project=project,
        key=key,
        by_api_key=api_key,
    )
    return {"deleted": True}
