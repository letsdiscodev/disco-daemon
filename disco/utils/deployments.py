import logging
import uuid
from typing import Literal

from sqlalchemy.orm.session import Session as DBSession

from disco.models import (
    ApiKey,
    Deployment,
    DeploymentEnvironmentVariable,
    Project,
)
from disco.utils import commandoutputs
from disco.utils.discofile import DiscoFile
from disco.utils.mq.tasks import enqueue_task

log = logging.getLogger(__name__)


def maybe_create_deployment(
    dbsession: DBSession,
    project: Project,
    commit_hash: str | None,
    disco_file: DiscoFile | None,
    by_api_key: ApiKey | None,
) -> Deployment | None:
    number = get_next_deployment_number(dbsession, project)
    if number == 1 and commit_hash is None and disco_file is None:
        return None
    return create_deployment(
        dbsession=dbsession,
        project=project,
        commit_hash=commit_hash,
        disco_file=disco_file,
        by_api_key=by_api_key,
    )


def create_deployment(
    dbsession: DBSession,
    project: Project,
    commit_hash: str | None,
    disco_file: DiscoFile | None,
    by_api_key: ApiKey | None,
) -> Deployment:
    number = get_next_deployment_number(dbsession, project)
    prev_deployment = get_live_deployment(dbsession, project)
    deployment = Deployment(
        id=uuid.uuid4().hex,
        number=number,
        prev_deployment_id=prev_deployment.id if prev_deployment is not None else None,
        project_name=project.name,
        domain=project.domain,
        github_repo=project.github_repo,
        github_host=project.github_host,
        project=project,
        status="QUEUED",
        commit_hash=commit_hash,
        disco_file=disco_file.model_dump_json(indent=2, by_alias=True)
        if disco_file is not None
        else None,
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
    log.info("Created deployment %s", deployment.log())
    enqueue_task(
        dbsession=dbsession,
        task_name="PROCESS_DEPLOYMENT",
        body=dict(
            deployment_id=deployment.id,
        ),
    )
    commandoutputs.save(
        dbsession, f"DEPLOYMENT_{deployment.id}", "Deployment enqueued\n"
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
    "COMPLETE",
    "FAILED",
]


def set_deployment_status(deployment: Deployment, status: BUILD_STATUS) -> None:
    log.info(
        "Setting deployment status of deployment %s to %s", deployment.log(), status
    )
    deployment.status = status


def set_deployment_disco_file(deployment: Deployment, disco_file: str) -> None:
    log.info("Setting deployment disco file of %s", deployment.log())
    deployment.disco_file = disco_file


def set_deployment_commit_hash(deployment: Deployment, commit_hash: str) -> None:
    log.info("Setting deployment commit_hash of %s: %s", deployment.log(), commit_hash)
    deployment.commit_hash = commit_hash


def get_live_deployment(dbsession: DBSession, project: Project) -> Deployment | None:
    return (
        dbsession.query(Deployment)
        .filter(Deployment.project == project)
        .filter(Deployment.status == "COMPLETE")
        .order_by(Deployment.number.desc())
        .first()
    )
