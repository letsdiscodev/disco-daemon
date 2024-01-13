import uuid
from typing import Literal

from sqlalchemy.orm.session import Session as DBSession

from disco.models import (
    ApiKey,
    Deployment,
    DeploymentEnvironmentVariable,
    DeploymentPublishedPort,
    DeploymentVolume,
    Project,
)
from disco.utils.mq.tasks import enqueue_task


def create_deployment(
    dbsession: DBSession,
    project: Project,
    pull: bool,
    image: str | None,
    by_api_key: ApiKey,
) -> Deployment | None:
    number = get_next_deployment_number(dbsession, project)
    if number == 1 and not pull and image is None:
        return None
    if not pull and image is None:
        image = project.deployments[0].image
    deployment = Deployment(
        id=uuid.uuid4().hex,
        number=number,
        project=project,
        status="QUEUED",
        pull=pull,
        image=image,
        by_api_key=by_api_key,
    )
    dbsession.add(deployment)
    for env_variable in project.env_variables:
        deploy_env_var = DeploymentEnvironmentVariable(
            id=uuid.uuid4().hex,
            name=env_variable.name,
            value=env_variable.value,
            deployment=deployment,
        )
        dbsession.add(deploy_env_var)
    for project_volume in project.volumes:
        deploy_volume = DeploymentVolume(
            id=uuid.uuid4().hex,
            deployment=deployment,
            name=project_volume.name,
            destination=project_volume.destination,
        )
        dbsession.add(deploy_volume)
    for project_published_port in project.published_ports:
        deploy_published_port = DeploymentPublishedPort(
            id=uuid.uuid4().hex,
            deployment=deployment,
            host_port=project_published_port.host_port,
            container_port=project_published_port.container_port,
        )
        dbsession.add(deploy_published_port)
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
    pull: bool,
    image: str | None,
    env_variables: list[tuple[str, str]],
    volumes: list[tuple[str, str]],
    published_ports: list[tuple[int, int]],
    exposed_ports: int | None,
    set_deployment_status,
) -> None:
    from disco.utils import caddy, docker, github

    assert not (pull and image is not None), "Can't pull when using image"
    if image is not None:
        # image deployment
        if not docker.image_exists(image):
            set_deployment_status("PULLING")
            docker.pull_image(image)
    elif pull:
        # git pull deployment
        set_deployment_status("PULLING")
        github.pull(
            project_name=project_name, github_repo=github_repo, github_host=github_host
        )
        set_deployment_status("BUILDING")
        docker.build_project(project_name, deployment_number)
        image = docker.image_name(project_name, deployment_number)
    if len(published_ports) > 0:
        if deployment_number > 1:
            # since the port is published, we have to stop the existing container
            # before starting the new one
            set_deployment_status("STOPPING_CONTAINER")
            docker.stop_container(project_name, deployment_number - 1)
        set_deployment_status("STARTING_CONTAINER")
        container = docker.container_name(project_name, deployment_number)
        docker.start_container(
            image=image,
            container=container,
            env_variables=env_variables,
            volumes=volumes,
            published_ports=published_ports,
            exposed_ports=exposed_ports,
        )
        if project_domain is not None:
            caddy.serve_container(
                project_name,
                project_domain,
                docker.container_name(project_name, deployment_number),
            )
        if deployment_number > 1:
            set_deployment_status("CLEAN_UP")
            docker.remove_container(project_name, deployment_number - 1)
    else:
        set_deployment_status("STARTING_CONTAINER")
        container = docker.container_name(project_name, deployment_number)
        docker.start_container(
            image=image,
            container=container,
            env_variables=env_variables,
            volumes=volumes,
            published_ports=[],
            exposed_ports=exposed_ports,
        )
        if project_domain is not None:
            caddy.serve_container(
                project_name,
                project_domain,
                docker.container_name(project_name, deployment_number),
            )
        if deployment_number > 1:
            set_deployment_status("CLEAN_UP")
            docker.stop_container(project_name, deployment_number - 1)
            docker.remove_container(project_name, deployment_number - 1)
    set_deployment_status("DONE")
