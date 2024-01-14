import logging
import uuid

from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, Project
from disco.utils.caddy import add_project_route
from disco.utils.sshkeys import create_deploy_key

log = logging.getLogger(__name__)


def create_project(
    dbsession: DBSession,
    name: str,
    github_repo: str | None,
    domain: str | None,
    by_api_key: ApiKey,
) -> tuple[Project, str]:
    project = Project(
        id=uuid.uuid4().hex,
        name=name,
        github_repo=github_repo,
        domain=domain,
        ssh_key_name=name,
    )
    github_host, ssh_key_pub = create_deploy_key(name)
    project.github_host = github_host
    dbsession.add(project)
    if domain is not None:
        add_project_route(project_id=project.id, domain=project.domain)
    log.info("%s created project %s", by_api_key.log(), project.log())
    return project, ssh_key_pub


def get_project_by_id(dbsession: DBSession, project_id: str) -> Project | None:
    return dbsession.query(Project).get(project_id)


def get_project_by_name(dbsession: DBSession, name: str) -> Project | None:
    return dbsession.query(Project).filter(Project.name == name).first()


def get_all_projects(dbsession: DBSession) -> list[Project]:
    return dbsession.query(Project).all()
