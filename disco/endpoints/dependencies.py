from typing import Annotated

from fastapi import Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models.db import AsyncSession, Session
from disco.utils.projects import get_project_by_name, get_project_by_name_sync


def get_sync_db():
    with Session.begin() as dbsession:
        yield dbsession


async def get_db():
    async with AsyncSession.begin() as dbsession:
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
    dbsession: Annotated[DBSession, Depends(get_sync_db)],
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
