import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, Project, ProjectEnvironmentVariable
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


def get_env_variables_for_project_sync(
    dbsession: DBSession, project: Project
) -> list[ProjectEnvironmentVariable]:
    return (
        dbsession.query(ProjectEnvironmentVariable)
        .filter(ProjectEnvironmentVariable.project == project)
        .order_by(ProjectEnvironmentVariable.name)
        .all()
    )


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
        if not existed:
            env_variable = ProjectEnvironmentVariable(
                id=uuid.uuid4().hex,
                name=name,
                value=encrypt(value),
                project=project,
                by_api_key=by_api_key,
            )
            dbsession.add(env_variable)


async def delete_env_variable(
    dbsession: AsyncDBSession,
    env_variable: ProjectEnvironmentVariable,
) -> None:
    await dbsession.delete(env_variable)
