from typing import Annotated

from fastapi import HTTPException, Path

from disco.models.db import AsyncReadSession
from disco.utils.projects import get_project_by_name


async def get_project_name_from_url_wo_tx(
    project_name: Annotated[str, Path()],
):
    async with AsyncReadSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
    yield project_name
