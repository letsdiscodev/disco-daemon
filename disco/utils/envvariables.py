import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession

from disco.models import ApiKey, Project, ProjectEnvironmentVariable
from disco.utils import events
from disco.utils.encryption import encrypt


async def get_env_variable_by_name(
    dbsession: AsyncDBSession,
    project: Project,
    name: str,
) -> ProjectEnvironmentVariable | None:
    stmt = (
        select(ProjectEnvironmentVariable)
        .where(ProjectEnvironmentVariable.project == project)
        .where(ProjectEnvironmentVariable.name == name)
        .limit(1)
    )
    result = await dbsession.execute(stmt)
    return result.scalars().first()


async def get_env_variables_for_project(
    dbsession: AsyncDBSession, project: Project
) -> list[ProjectEnvironmentVariable]:
    stmt = (
        select(ProjectEnvironmentVariable)
        .where(ProjectEnvironmentVariable.project == project)
        .order_by(ProjectEnvironmentVariable.name)
    )
    result = await dbsession.execute(stmt)
    return list(result.scalars().all())


async def set_env_variables(
    dbsession: AsyncDBSession,
    project: Project,
    env_variables: list[tuple[str, str]],
    by_api_key: ApiKey,
) -> None:
    for name, value in env_variables:
        existed = False
        for env_variable in await project.awaitable_attrs.env_variables:
            if env_variable.name == name:
                existed = True
                env_variable.value = encrypt(value)
                env_variable.by_api_key = by_api_key
                events.env_variable_updated(project_name=project.name, env_var=name)
        if not existed:
            env_variable = ProjectEnvironmentVariable(
                id=uuid.uuid4().hex,
                name=name,
                value=encrypt(value),
                project=project,
                by_api_key=by_api_key,
            )
            dbsession.add(env_variable)
            events.env_variable_created(project_name=project.name, env_var=name)


async def delete_env_variable(
    dbsession: AsyncDBSession,
    env_variable: ProjectEnvironmentVariable,
) -> None:
    project: Project = await env_variable.awaitable_attrs.project
    events.env_variable_removed(project_name=project.name, env_var=env_variable.name)
    await dbsession.delete(env_variable)


async def delete_env_variables_by_name(
    dbsession: AsyncDBSession,
    project: Project,
    names: list[str],
) -> int:
    """Delete env vars matching any of the given names. Names that don't
    exist on the project are silently skipped. Returns the number of vars
    actually deleted, so callers can suppress no-op deployments."""
    name_set = set(names)
    deleted = 0
    for env_variable in list(await project.awaitable_attrs.env_variables):
        if env_variable.name in name_set:
            await delete_env_variable(dbsession, env_variable)
            deleted += 1
    return deleted
