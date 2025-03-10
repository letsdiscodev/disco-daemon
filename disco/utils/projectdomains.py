import logging
import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, Project, ProjectDomain
from disco.utils import caddy, docker, events
from disco.utils.deployments import get_live_deployment
from disco.utils.discofile import ServiceType, get_disco_file_from_str

log = logging.getLogger(__name__)


async def add_domain(
    dbsession: AsyncDBSession,
    project: Project,
    domain_name: str,
    by_api_key: ApiKey,
) -> ProjectDomain:
    domain = ProjectDomain(
        id=uuid.uuid4().hex,
        name=domain_name,
        project=project,
    )
    dbsession.add(domain)
    log.info(
        "%s added domain to project: %s %s",
        by_api_key.log(),
        project.log(),
        domain.log(),
    )

    www_apex_domain_name = _get_apex_www_redirect_for_domain(domain_name)
    www_apex_domain = (
        (await get_domain_by_name(dbsession, www_apex_domain_name))
        if www_apex_domain_name is not None
        else None
    )
    if www_apex_domain is not None:
        # we're adding example.com, but
        # www.example.com already had a redirect from example.com
        log.info(
            "Removing domain redirect from %s to %s",
            www_apex_domain_name,
            www_apex_domain.name,
        )
        await caddy.remove_apex_www_redirects(www_apex_domain.id)
    await _update_caddy_domains_for_project(dbsession, project)
    if www_apex_domain_name is not None and www_apex_domain is None:
        # we're adding www.example.com and example.com is free
        log.info(
            "Adding domain redirect from %s to %s", www_apex_domain_name, domain.name
        )
        await caddy.add_apex_www_redirects(
            domain_id=domain.id,
            from_domain=www_apex_domain_name,
            to_domain=domain.name,
        )
    project_domains = await project.awaitable_attrs.domains
    if len(project_domains) == 1:
        # just added first domain, need to set what it's serving
        assert project_domains[0] == domain
        await serve_live_deployment(dbsession, project)
    events.domain_created(project_name=project.name, domain=domain.name)
    return domain


async def remove_domain(
    dbsession: AsyncDBSession, domain: ProjectDomain, by_api_key: ApiKey
) -> None:
    project = await domain.awaitable_attrs.project
    domain_id = domain.id
    domain_name = domain.name
    log.info(
        "%s is removing domain from project: %s %s",
        by_api_key.log(),
        project.log(),
        domain.log(),
    )
    await dbsession.delete(domain)
    events.domain_removed(project_name=project.name, domain=domain_name)
    await _update_caddy_domains_for_project(dbsession, project)
    www_apex_domain_name = _get_apex_www_redirect_for_domain(domain_name)
    www_apex_domain = (
        (await get_domain_by_name(dbsession, www_apex_domain_name))
        if www_apex_domain_name is not None
        else None
    )
    if www_apex_domain_name is not None:
        if www_apex_domain is None:
            # removing www.example.com and example.com doesn't exist,
            # meaning we had a redirect we should remove
            log.info(
                "Removing domain redirect from %s to %s",
                www_apex_domain_name,
                domain_name,
            )
            await caddy.remove_apex_www_redirects(domain_id)
        else:
            # removing www.example.com and example.com exists,
            # meaning we're freeing www.example.com so we should create a redirect
            await caddy.add_apex_www_redirects(
                domain_id=www_apex_domain.id,
                from_domain=domain_name,
                to_domain=www_apex_domain.name,
            )


async def get_domains_for_project(
    dbsession: AsyncDBSession, project: Project
) -> Sequence[ProjectDomain]:
    stmt = select(ProjectDomain).where(ProjectDomain.project == project)
    result = await dbsession.execute(stmt)
    return result.scalars().all()


async def _update_caddy_domains_for_project(
    dbsession: AsyncDBSession, project: Project
) -> None:
    project_domains = await get_domains_for_project(dbsession, project)
    domains = [d.name for d in project_domains]
    log.info("\n\n\n\n\n\n\n\nDomains: %s\n\n\n\n\n", domains)
    await caddy.set_domains_for_project(project_name=project.name, domains=domains)


def _get_apex_www_redirect_for_domain(domain_name: str) -> str | None:
    parts = domain_name.split(".")
    if len(parts) == 2:
        # example.com, return www.example.com
        return ".".join(["www"] + parts)
    if len(parts) == 3 and parts[0] == "www":
        # www.example.com, return example.com
        return ".".join(parts[1:])
    # site.example.com, or a.site.example.com, return None
    return None


async def get_domain_by_id(
    dbsession: AsyncDBSession, domain_id: str
) -> ProjectDomain | None:
    return await dbsession.get(ProjectDomain, domain_id)


async def get_domain_by_name(
    dbsession: AsyncDBSession, domain_name: str
) -> ProjectDomain | None:
    stmt = select(ProjectDomain).where(ProjectDomain.name == domain_name).limit(1)
    result = await dbsession.execute(stmt)
    return result.scalars().first()


def get_domain_by_name_sync(
    dbsession: DBSession, domain_name: str
) -> ProjectDomain | None:
    stmt = select(ProjectDomain).where(ProjectDomain.name == domain_name).limit(1)
    result = dbsession.execute(stmt)
    return result.scalars().first()


async def serve_live_deployment(dbsession: AsyncDBSession, project: Project) -> None:
    deployment = await get_live_deployment(dbsession, project)
    if deployment is None:
        return  # nothing to serve
    if deployment.disco_file is None:
        return  # nothing to serve
    disco_file = get_disco_file_from_str(deployment.disco_file)
    if "web" not in disco_file.services:
        return  # nothing to serve
    if disco_file.services["web"].type == ServiceType.container:
        internal_service_name = docker.service_name(
            project.name, "web", deployment.number
        )
        await caddy.serve_service(
            project.name,
            internal_service_name,
            port=disco_file.services["web"].port or 8000,
        )
    elif disco_file.services["web"].type == ServiceType.static:
        await caddy.serve_static_site(project.name, deployment.number)
