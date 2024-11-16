import asyncio
import logging
import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models import (
    ApiKey,
    Project,
    ProjectDomain,
    ProjectGithubRepo,
)
from disco.utils import docker, github
from disco.utils.commandoutputs import delete_output_for_source, deployment_source
from disco.utils.filesystem import remove_project_static_deployments_if_any
from disco.utils.projectdomains import remove_domain_sync

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


def get_all_projects(dbsession: DBSession) -> list[Project]:
    return dbsession.query(Project).order_by(Project.name).all()


def delete_project(dbsession: DBSession, project: Project, by_api_key: ApiKey) -> None:
    from disco.utils.asyncworker import async_worker

    log.info("%s is deleting project %s", by_api_key.log(), project.log())
    if project.github_repo is not None:
        try:
            github.remove_repo(project.name)
        except Exception:
            log.info("Failed to remove Github repo for project %s", project.name)
    remove_project_static_deployments_if_any(project.name)
    for domain in project.domains:
        remove_domain_sync(dbsession=dbsession, domain=domain, by_api_key=by_api_key)
    services = docker.list_services_for_project_sync(project.name)
    for service_name in services:
        try:
            docker.stop_service_sync(service_name)
        except Exception:
            log.info("Failed to stop service %s", service_name)
    containers = docker.list_containers_for_project(project.name)
    for container in containers:
        docker.remove_container_sync(container)
    networks = asyncio.run(docker.list_networks_for_project(project.name))
    for network in networks:
        try:
            docker.remove_network_from_container("disco-caddy", network)
        except Exception:
            pass
        try:
            docker.remove_network_sync(network)
        except Exception:
            log.info("Failed to remove network %s", network)
    async_worker.remove_project_crons(project.name)
    if project.github_repo is not None:
        dbsession.delete(project.github_repo)
    for p_env_var in project.env_variables:
        dbsession.delete(p_env_var)
    for deployment in project.deployments:
        delete_output_for_source(deployment_source(deployment.id))
        for d_env_var in deployment.env_variables:
            dbsession.delete(d_env_var)
        dbsession.delete(deployment)
    for keyvalue in project.key_values:
        dbsession.delete(keyvalue)
    for run in project.command_runs:
        dbsession.delete(run)
    dbsession.delete(project)


def volume_name_for_project(name: str, project_id: str) -> str:
    return f"disco-project-{project_id}-{name}"
