import logging
import uuid
from typing import Literal, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models import (
    ApiKey,
    Deployment,
    DeploymentEnvironmentVariable,
    Project,
)
from disco.models.db import AsyncSession
from disco.utils import commandoutputs, events, keyvalues
from disco.utils.discofile import DiscoFile

log = logging.getLogger(__name__)


async def maybe_create_deployment(
    dbsession: AsyncDBSession,
    project: Project,
    commit_hash: str | None,
    disco_file: DiscoFile | None,
    by_api_key: ApiKey | None,
) -> Deployment | None:
    number = await get_next_deployment_number(dbsession, project)
    if number == 1 and commit_hash is None and disco_file is None:
        return None
    return await create_deployment(
        dbsession=dbsession,
        project=project,
        commit_hash=commit_hash,
        disco_file=disco_file,
        by_api_key=by_api_key,
    )


async def create_deployment(
    dbsession: AsyncDBSession,
    project: Project,
    commit_hash: str | None,
    disco_file: DiscoFile | None,
    by_api_key: ApiKey | None,
    number: int | None = None,
) -> Deployment:
    if number is not None:
        if len(await project.awaitable_attrs.deployments) > 0:
            raise Exception(
                "Cannot set deployment number if project already has deployments"
            )
    else:
        number = await get_next_deployment_number(dbsession, project)
    prev_deployment = await get_live_deployment(dbsession, project)
    project_github_repo = await project.awaitable_attrs.github_repo
    deployment = Deployment(
        id=uuid.uuid4().hex,
        number=number,
        prev_deployment_id=prev_deployment.id if prev_deployment is not None else None,
        project_name=project.name,
        project=project,
        github_repo_full_name=project_github_repo.full_name
        if project_github_repo is not None
        else None,
        branch=project_github_repo.branch if project_github_repo is not None else None,
        status="QUEUED",
        commit_hash=commit_hash,
        disco_file=disco_file.model_dump_json(indent=2, by_alias=True)
        if disco_file is not None
        else None,
        registry_host=await keyvalues.get_value(dbsession, "REGISTRY_HOST"),
        by_api_key=by_api_key,
    )
    dbsession.add(deployment)
    for env_variable in await project.awaitable_attrs.env_variables:
        deploy_env_var = DeploymentEnvironmentVariable(
            id=uuid.uuid4().hex,
            name=env_variable.name,
            value=env_variable.value,
            deployment=deployment,
        )
        dbsession.add(deploy_env_var)
    log.info("Created deployment %s", deployment.log())
    await commandoutputs.init(commandoutputs.deployment_source(deployment.id))
    await commandoutputs.store_output(
        commandoutputs.deployment_source(deployment.id),
        f"Deployment {deployment.number} enqueued\n",
    )
    events.deployment_created(
        project_name=project.name,
        deployment_number=deployment.number,
        status="QUEUED",
    )
    return deployment


async def get_next_deployment_number(
    dbsession: AsyncDBSession, project: Project
) -> int:
    stmt = (
        select(Deployment)
        .where(Deployment.project == project)
        .order_by(Deployment.number.desc())
        .limit(1)
    )
    result = await dbsession.execute(stmt)
    deployment = result.scalars().first()
    if deployment is None:
        number = 0
    else:
        number = deployment.number
    return number + 1


async def get_deployment_by_id(
    dbsession: AsyncDBSession, deployment_id: str
) -> Deployment | None:
    return await dbsession.get(Deployment, deployment_id)


def get_deployment_by_number_sync(
    dbsession: DBSession, project: Project, deployment_number: int
) -> Deployment | None:
    return (
        dbsession.query(Deployment)
        .filter(Deployment.project == project)
        .filter(Deployment.number == deployment_number)
        .first()
    )


async def get_deployment_by_number(
    dbsession: AsyncDBSession, project: Project, deployment_number: int
) -> Deployment | None:
    stmt = (
        select(Deployment)
        .where(Deployment.project == project)
        .where(Deployment.number == deployment_number)
    )
    result = await dbsession.execute(stmt)
    return result.scalars().first()


DEPLOYMENT_STATUS = Literal[
    "QUEUED",
    "PREPARING",
    "REPLACING",
    "COMPLETE",
    "SKIPPED",
    "FAILED",
    "CANCELLING",
    "CANCELLED",
]


async def set_deployment_status(
    deployment: Deployment, status: DEPLOYMENT_STATUS
) -> None:
    log.info(
        "Setting deployment status of deployment %s to %s", deployment.log(), status
    )
    deployment.status = status
    project: Project = await deployment.awaitable_attrs.project
    events.deployment_status(
        project_name=project.name,
        deployment_number=deployment.number,
        status=deployment.status,
    )


async def get_deployments_with_status(
    dbsession: AsyncDBSession, project: Project, status: DEPLOYMENT_STATUS
) -> Sequence[Deployment]:
    stmt = (
        select(Deployment)
        .where(Deployment.project == project)
        .where(Deployment.status == status)
        .order_by(Deployment.number)
    )
    result = await dbsession.execute(stmt)
    return result.scalars().all()


def set_deployment_task_id(deployment: Deployment, task_id: str) -> None:
    deployment.task_id = task_id


async def cancel_deployment(deployment: Deployment, by_api_key: ApiKey) -> bool:
    """Cancels a deployment.

    Returns True if should process next deployment.
    It's the case when a deployment was in progress but wasn't
    actually found in the tasks. It could happen if something
    doesn't work and is there as a fallback to fix the project,
    in case it would be stuck with a broken deployment forever.

    """
    from disco.utils.asyncworker import async_worker

    assert deployment.status in ["QUEUED", "PREPARING", "REPLACING", "CANCELLING"]

    log.info(
        "Cancelling deployment %s (had status %s) by %s",
        deployment.id,
        deployment.status,
        by_api_key.log(),
    )
    output_source = commandoutputs.deployment_source(deployment.id)
    await commandoutputs.store_output(
        output_source,
        "Cancelling build - initiated by API key: "
        f"{by_api_key.public_key} ({by_api_key.name})\n",
    )
    if deployment.status == "QUEUED":
        await commandoutputs.store_output(output_source, "Cancelled\n")
        await commandoutputs.terminate(output_source)
        await set_deployment_status(deployment, "CANCELLED")
        return False
    elif deployment.status in ["PREPARING", "REPLACING", "CANCELLING"]:
        from disco.utils.asyncworker import TaskNotFoundError

        assert deployment.task_id is not None
        try:
            async_worker.cancel_task(deployment.task_id)
            return False
        except TaskNotFoundError:
            await commandoutputs.store_output(output_source, "Cancelled\n")
            await commandoutputs.terminate(output_source)
            await set_deployment_status(deployment, "CANCELLED")
            return True
    else:
        raise NotImplementedError(f"Status {deployment.status}")


async def cleanup_deployments_on_disco_boot() -> None:
    async with AsyncSession.begin() as dbsession:
        stmt = select(Deployment).where(
            Deployment.status.in_(["PREPARING", "REPLACING", "CANCELLING"])
        )
        result = await dbsession.execute(stmt)
        deployments = result.scalars().all()
        for deployment in deployments:
            output_source = commandoutputs.deployment_source(deployment.id)
            await commandoutputs.store_output(
                output_source, "Marked as failed on Disco start\n"
            )
            await commandoutputs.terminate(output_source)
            await set_deployment_status(deployment, "FAILED")


async def enqueue_deployments_on_disco_boot() -> None:
    from disco.utils.deploymentflow import process_deployment_if_any
    from disco.utils.projects import get_all_projects

    async with AsyncSession.begin() as dbsession:
        projects = await get_all_projects(dbsession)
        project_ids = [project.id for project in projects]
    for project_id in project_ids:
        await process_deployment_if_any(project_id)


def set_deployment_disco_file(deployment: Deployment, disco_file: str) -> None:
    log.info("Setting deployment disco file of %s", deployment.log())
    deployment.disco_file = disco_file


def set_deployment_commit_hash(deployment: Deployment, commit_hash: str) -> None:
    log.info("Setting deployment commit_hash of %s: %s", deployment.log(), commit_hash)
    deployment.commit_hash = commit_hash


async def get_live_deployment(
    dbsession: AsyncDBSession, project: Project
) -> Deployment | None:
    stmt = (
        select(Deployment)
        .where(Deployment.project == project)
        .where(Deployment.status == "COMPLETE")
        .order_by(Deployment.number.desc())
        .limit(1)
    )
    result = await dbsession.execute(stmt)
    return result.scalars().first()


def get_live_deployment_sync(
    dbsession: DBSession, project: Project
) -> Deployment | None:
    return (
        dbsession.query(Deployment)
        .filter(Deployment.project == project)
        .filter(Deployment.status == "COMPLETE")
        .order_by(Deployment.number.desc())
        .first()
    )


async def get_last_deployment(
    dbsession: AsyncDBSession,
    project: Project,
    statuses: list[DEPLOYMENT_STATUS] | None = None,
) -> Deployment | None:
    stmt = select(Deployment).where(Deployment.project == project)
    if statuses is not None:
        stmt = stmt.where(Deployment.status.in_(statuses))
    stmt = stmt.order_by(Deployment.number.desc()).limit(1)
    result = await dbsession.execute(stmt)
    return result.scalars().first()


async def get_deployment_in_progress(
    dbsession: AsyncDBSession, project: Project
) -> Deployment | None:
    stmt = (
        select(Deployment)
        .where(Deployment.project == project)
        .where(Deployment.status.in_(["PREPARING", "REPLACING"]))
        .order_by(Deployment.number.desc())
        .limit(1)
    )
    result = await dbsession.execute(stmt)
    return result.scalars().first()


async def get_oldest_queued_deployment(
    dbsession: AsyncDBSession, project: Project
) -> Deployment | None:
    stmt = (
        select(Deployment)
        .where(Deployment.project == project)
        .where(Deployment.status == "QUEUED")
        .order_by(Deployment.number.asc())
        .limit(1)
    )
    result = await dbsession.execute(stmt)
    return result.scalars().first()
