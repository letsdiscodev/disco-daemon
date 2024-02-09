from typing import Annotated

from anyio import Path
from fastapi import Depends, HTTPException
from sqlalchemy.orm.session import Session as DBSession

from disco.models.db import Session
from disco.utils.projects import get_project_by_name


def get_db():
    with Session() as dbsession:
        with dbsession.begin():
            yield dbsession


def get_project_from_url(
    project_name: Annotated[str, Path()],
    dbsession: Annotated[DBSession, Depends(get_db)],
):
    project = get_project_by_name(dbsession, project_name)
    if project is None:
        raise HTTPException(status_code=404)
    yield project
