from contextlib import AsyncExitStack
from typing import Annotated

from fastapi import Depends, HTTPException, Path, Request
from fastapi.concurrency import contextmanager_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models.db import AsyncSession, Session
from disco.utils.projects import get_project_by_name, get_project_by_name_sync


async def get_db_sync(request: Request):
    function_astack: AsyncExitStack = request.scope["fastapi_function_astack"]
    dbsession = await function_astack.enter_async_context(
        contextmanager_in_threadpool(Session.begin())
    )
    yield dbsession


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


def get_project_from_url_sync(
    project_name: Annotated[str, Path()],
    dbsession: Annotated[DBSession, Depends(get_db_sync)],
):
    project = get_project_by_name_sync(dbsession, project_name)
    if project is None:
        raise HTTPException(status_code=404)
    yield project


async def get_project_from_url(
    project_name: Annotated[str, Path()],
    dbsession: Annotated[AsyncDBSession, Depends(get_db)],
):
    project = await get_project_by_name(dbsession, project_name)
    if project is None:
        raise HTTPException(status_code=404)
    yield project
