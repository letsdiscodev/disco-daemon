import logging
import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models import (
    ApiKey,
    CommandRun,
    Deployment,
    DeploymentEnvironmentVariable,
    Project,
    ProjectDomain,
    ProjectEnvironmentVariable,
    ProjectGithubRepo,
    ProjectKeyValue,
)
from disco.utils import docker, events, github
from disco.utils.commandoutputs import delete_output_for_source, deployment_source
from disco.utils.filesystem import remove_project_static_deployments_if_any
from disco.utils.projectdomains import remove_domain

log = logging.getLogger(__name__)


def create_project(
    dbsession: AsyncDBSession,
    name: str,
    by_api_key: ApiKey,
) -> Project:
    project = Project(
        id=uuid.uuid4().hex,
        name=name,
    )
    dbsession.add(project)
    log.info("%s created project %s", by_api_key.log(), project.log())
    events.project_created(project_name=name)
    return project


async def set_project_github_repo(
    dbsession: AsyncDBSession,
    project: Project,
    github_repo: str,
    branch: str | None,
    by_api_key: ApiKey,
):
    log.info(
        "%s is setting project Github repo %s %s (branch %s)",
        by_api_key.log(),
        project.log(),
        github_repo,
        branch,
    )
    if project.deployment_type is not None:
        if project.deployment_type == "GITHUB":
            await dbsession.delete(project.github_repo)
        else:
            raise NotImplementedError(f"{project.deployment_type} not handled")

    project.deployment_type = "GITHUB"
    project.github_repo = ProjectGithubRepo(
        id=uuid.uuid4().hex,
        full_name=github_repo,
        branch=branch,
    )


def get_project_by_id_sync(dbsession: DBSession, project_id: str) -> Project | None:
    return dbsession.query(Project).get(project_id)


async def get_project_by_id(
    dbsession: AsyncDBSession, project_id: str
) -> Project | None:
    return await dbsession.get(Project, project_id)


def get_project_by_name_sync(dbsession: DBSession, name: str) -> Project | None:
    return dbsession.query(Project).filter(Project.name == name).first()


async def get_project_by_name(dbsession: AsyncDBSession, name: str) -> Project | None:
    stmt = select(Project).where(Project.name == name).limit(1)
    result = await dbsession.execute(stmt)
    return result.scalars().first()


def get_project_by_domain_sync(dbsession: DBSession, domain: str) -> Project | None:
    return (
        dbsession.query(Project)
        .join(ProjectDomain)
        .filter(ProjectDomain.name == domain)
        .first()
    )


async def get_project_by_domain(
    dbsession: AsyncDBSession, domain: str
) -> Project | None:
    stmt = (
        select(Project).join(ProjectDomain).where(ProjectDomain.name == domain).limit(1)
    )
    result = await dbsession.execute(stmt)
    return result.scalars().first()


async def get_projects_by_github_app_repo(
    dbsession: AsyncDBSession, full_name: str
) -> Sequence[Project]:
    stmt = (
        select(Project)
        .join(ProjectGithubRepo)
        .where(ProjectGithubRepo.full_name == full_name)
    )
    result = await dbsession.execute(stmt)
    return result.scalars().all()


async def get_all_projects(dbsession: AsyncDBSession) -> Sequence[Project]:
    stmt = select(Project).order_by(Project.name)
    result = await dbsession.execute(stmt)
    return result.scalars().all()


def get_all_projects_sync(dbsession: DBSession) -> list[Project]:
    return dbsession.query(Project).order_by(Project.name).all()


async def delete_project(
    dbsession: AsyncDBSession, project: Project, by_api_key: ApiKey
) -> None:
    from disco.utils.asyncworker import async_worker

    log.info("%s is deleting project %s", by_api_key.log(), project.log())
    github_repo: ProjectGithubRepo | None = await project.awaitable_attrs.github_repo
    if github_repo is not None:
        try:
            await github.remove_repo_from_filesystem(project.name)
        except Exception:
            log.info("Failed to remove Github repo for project %s", project.name)
    await remove_project_static_deployments_if_any(project.name)
    p_domains: list[ProjectDomain] = list(await project.awaitable_attrs.domains)
    for domain in p_domains:
        await remove_domain(dbsession=dbsession, domain=domain, by_api_key=by_api_key)
    services = await docker.list_services_for_project(project.name)
    for service_name in services:
        try:
            await docker.rm_service(service_name)
        except Exception:
            log.info("Failed to stop service %s", service_name)
    containers = await docker.list_containers_for_project(project.name)
    for container in containers:
        await docker.remove_container(container)
    networks = await docker.list_networks_for_project(project.name)
    for network in networks:
        try:
            await docker.remove_network_from_container("disco-caddy", network)
        except Exception:
            pass
    async_worker.remove_project_crons(project.name)
    if github_repo is not None:
        await dbsession.delete(github_repo)
    p_env_vars: Sequence[
        ProjectEnvironmentVariable
    ] = await project.awaitable_attrs.env_variables
    for p_env_var in p_env_vars:
        await dbsession.delete(p_env_var)
    deployments: Sequence[Deployment] = await project.awaitable_attrs.deployments
    for deployment in deployments:
        await delete_output_for_source(deployment_source(deployment.id))
        d_env_vars: Sequence[
            DeploymentEnvironmentVariable
        ] = await deployment.awaitable_attrs.env_variables
        for d_env_var in d_env_vars:
            await dbsession.delete(d_env_var)
        await dbsession.delete(deployment)
    p_key_values: Sequence[ProjectKeyValue] = await project.awaitable_attrs.key_values
    for keyvalue in p_key_values:
        await dbsession.delete(keyvalue)
    command_runs: Sequence[CommandRun] = await project.awaitable_attrs.command_runs
    for run in command_runs:
        await dbsession.delete(run)
    events.project_removed(project_name=project.name)
    await dbsession.delete(project)


def volume_name_for_project(name: str, project_id: str) -> str:
    return f"disco-project-{project_id}-{name}"
