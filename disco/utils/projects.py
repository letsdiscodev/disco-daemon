import uuid

from sqlalchemy.orm.session import Session as DBSession
from disco.models import Project

import logging

log = logging.getLogger(__name__)


def create_project(dbsession: DBSession, name: str, github_repo: str) -> Project:
    project = Project(
        id=uuid.uuid4().hex,
        name=name,
        github_repo=github_repo,
    )
    dbsession.add(project)
    # TODO "by_api_key"
    log.info("Created project %s", project.log())
    return project


def get_project_by_id(dbsession: DBSession, project_id: str) -> Project | None:
    return dbsession.query(Project).get(project_id)


def get_all_projects(dbsession: DBSession) -> list[Project]:
    return dbsession.query(Project).all()
