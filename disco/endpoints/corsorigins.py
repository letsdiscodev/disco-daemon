import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from disco.auth import get_api_key
from disco.endpoints.dependencies import get_db
from disco.models import ApiKey
from disco.utils.corsorigins import allow_origin, get_all_cors_origins

log = logging.getLogger(__name__)

router = APIRouter()


class AddCorsOriginRequestBody(BaseModel):
    origin: str = Field(..., max_length=1024)


@router.get("/api/cors/origins", status_code=200, dependencies=[Depends(get_api_key)])
async def cors_origins_get(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
):
    cors_origins = await get_all_cors_origins(dbsession)
    return {
        "corsOrigins": [
            {
                "id": o.id,
                "origin": o.origin,
            }
            for o in cors_origins
        ],
    }


@router.post("/api/cors/origins", status_code=200)
async def cors_origins_post(
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
    api_key: Annotated[ApiKey, Depends(get_api_key)],
    req_body: AddCorsOriginRequestBody,
):
    await allow_origin(dbsession=dbsession, origin=req_body.origin, by_api_key=api_key)
    cors_origins = await get_all_cors_origins(dbsession)
    return {
        "corsOrigins": [
            {
                "id": o.id,
                "origin": o.origin,
            }
            for o in cors_origins
        ],
    }
