import uuid

from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, Deployment, Project, ProjectPublishedPort
from disco.utils.deployments import create_deployment


def add_published_port(
    dbsession: DBSession,
    project: Project,
    host_port: int,
    container_port: int,
    protocol: str,
    by_api_key: ApiKey,
) -> Deployment | None:
    # TODO if the port is already published, do nothing
    project_volume = ProjectPublishedPort(
        id=uuid.uuid4().hex,
        project=project,
        host_port=host_port,
        container_port=container_port,
        protocol=protocol,
        by_api_key=by_api_key,
    )
    dbsession.add(project_volume)
    return create_deployment(
        dbsession=dbsession,
        project=project,
        pull=False,
        image=None,
        by_api_key=by_api_key,
    )


def remove_published_port(
    dbsession: DBSession, published_port: ProjectPublishedPort, by_api_key: ApiKey
) -> Deployment | None:
    project = published_port.project
    dbsession.delete(published_port)
    dbsession.flush()
    dbsession.expire(project, ["published_ports"])
    return create_deployment(
        dbsession=dbsession,
        project=project,
        pull=False,
        image=None,
        by_api_key=by_api_key,
    )
