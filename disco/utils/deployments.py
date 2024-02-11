import uuid
from typing import Literal

from sqlalchemy.orm.session import Session as DBSession

from disco.models import (
    ApiKey,
    Deployment,
    DeploymentEnvironmentVariable,
    Project,
)
from disco.utils.mq.tasks import enqueue_task


def create_deployment(
    dbsession: DBSession,
    project: Project,
    commit_hash: str | None,
    disco_config: str | None,
    by_api_key: ApiKey,
) -> Deployment | None:
    number = get_next_deployment_number(dbsession, project)
    if number == 1 and commit_hash is None and disco_config is None:
        return None
    deployment = Deployment(
        id=uuid.uuid4().hex,
        number=number,
        project_name=project.name,
        project=project,
        status="QUEUED",
        commit_hash=commit_hash,
        disco_config=disco_config,
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


def get_deployment_by_number(
    dbsession: DBSession, project: Project, deployment_number: int
) -> Deployment | None:
    return (
        dbsession.query(Deployment)
        .filter(Deployment.project == project)
        .filter(Deployment.number == deployment_number)
        .first()
    )


BUILD_STATUS = Literal[
    "QUEUED",
    "IN_PROGRESS",
    "SUCCESS",
    "FAILED",
]


def set_deployment_status(deployment: Deployment, status: BUILD_STATUS) -> None:
    deployment.status = status


def get_previous_deployment(
    dbsession: DBSession, deployment: Deployment
) -> Deployment | None:
    return (
        dbsession.query(Deployment)
        .filter(Deployment.project == deployment.project)
        .filter(Deployment.number == deployment.number - 1)
        .first()
    )
