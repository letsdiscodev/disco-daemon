import uuid
from sqlalchemy.orm.session import Session as DBSession

from disco.models import Deployment, Project, ApiKey


def create_deployment(
    dbsession: DBSession, project: Project, by_api_key: ApiKey
) -> Deployment:
    deployment = Deployment(
        id=uuid.uuid4().hex,
        number=get_next_deployment_number(dbsession, project),
        project=project,
        by_api_key=by_api_key,
    )
    dbsession.add(deployment)
    # TODO clean up, move rest to worker?
    from disco.utils import github, docker, caddy

    github.pull(project)
    docker.build_project(project, deployment.number)
    docker.start_container(project, deployment.number)
    caddy.serve_container(project, docker._container_name(project, deployment.number))
    if deployment.number > 1:
        docker.stop_container(project, deployment.number - 1)
        docker.remove_container(project, deployment.number - 1)
    return deployment


def get_next_deployment_number(dbsession: DBSession, project: Project) -> int:
    deployment = (
        dbsession.query(Deployment)
        .filter(Deployment.project == project)
        .order_by(Deployment.number.desc())
        .first()
    )
    if deployment is None:
        number = 0
    else:
        number = deployment.number
    return number + 1
