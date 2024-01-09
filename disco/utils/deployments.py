import uuid
from typing import Literal

from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, Deployment, Project
from disco.utils.mq.tasks import enqueue_task


def create_deployment(
    dbsession: DBSession, project: Project, by_api_key: ApiKey
) -> Deployment:
    deployment = Deployment(
        id=uuid.uuid4().hex,
        number=get_next_deployment_number(dbsession, project),
        project=project,
        status="QUEUED",
        by_api_key=by_api_key,
    )
    dbsession.add(deployment)
    enqueue_task(
        dbsession=dbsession,
        task_name="PROCESS_DEPLOYMENT",
        body=dict(
            deployment_id=deployment.id,
        ),
    )
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


def get_deployment_by_id(dbsession: DBSession, deployment_id: str) -> Deployment | None:
    return dbsession.query(Deployment).get(deployment_id)


BUILD_STATUS = Literal[
    "QUEUED", "STARTED", "PULLING", "BUILDING", "STARTING_CONTAINER", "CLEAN_UP", "DONE"
]


def set_deployment_status(deployment: Deployment, status: BUILD_STATUS) -> None:
    deployment.status = status


def build(
    project_name: str,
    project_domain: str,
    github_repo: str,
    github_host: str,
    deployment_number: int,
    set_deployment_status,
) -> None:
    from disco.utils import caddy, docker, github

    set_deployment_status("PULLING")
    github.pull(
        project_name=project_name, github_repo=github_repo, github_host=github_host
    )
    set_deployment_status("BUILDING")
    docker.build_project(project_name, deployment_number)
    set_deployment_status("STARTING_CONTAINER")
    docker.start_container(project_name, deployment_number)
    caddy.serve_container(
        project_name,
        project_domain,
        docker._container_name(project_name, deployment_number),
    )
    if deployment_number > 1:
        set_deployment_status("CLEAN_UP")
        docker.stop_container(project_name, deployment_number - 1)
        docker.remove_container(project_name, deployment_number - 1)
    set_deployment_status("DONE")
