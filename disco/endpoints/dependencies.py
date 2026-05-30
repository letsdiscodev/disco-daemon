from contextlib import AsyncExitStack
from typing import Annotated

from fastapi import Depends, HTTPException, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from disco.models.db import AsyncSession
from disco.utils.projects import get_project_by_name


async def get_db(request: Request):
    function_astack: AsyncExitStack = request.scope["fastapi_function_astack"]
    dbsession = await function_astack.enter_async_context(AsyncSession.begin())
    yield dbsession


async def get_project_name_from_url_wo_tx(
    project_name: Annotated[str, Path()],
):
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
    yield project_name


async def get_project_from_url(
    project_name: Annotated[str, Path()],
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
):
    project = await get_project_by_name(dbsession, project_name)
    if project is None:
        raise HTTPException(status_code=404)
    yield project
