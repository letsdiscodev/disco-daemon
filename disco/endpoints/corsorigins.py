import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from disco.auth import get_api_key_wo_tx
from disco.models.db import ReadSession, Session
from disco.utils.apikeys import get_api_key_by_id
from disco.utils.corsorigins import allow_origin, get_all_cors_origins

log = logging.getLogger(__name__)

router = APIRouter()


class AddCorsOriginRequestBody(BaseModel):
    origin: str = Field(..., max_length=1024)


@router.get(
    "/api/cors/origins", status_code=200, dependencies=[Depends(get_api_key_wo_tx)]
)
async def cors_origins_get():
    async with ReadSession.begin() as dbsession:
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
    api_key_id: Annotated[str, Depends(get_api_key_wo_tx)],
    req_body: AddCorsOriginRequestBody,
):
    async with Session.begin() as dbsession:
        api_key = await get_api_key_by_id(dbsession, api_key_id)
        assert api_key is not None
        await allow_origin(
            dbsession=dbsession, origin=req_body.origin, by_api_key=api_key
        )
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
