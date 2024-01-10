import uuid

from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, Deployment, Project, ProjectEnvironmentVariable
from disco.utils.deployments import create_deployment


def get_env_variable_by_name(
    dbsession: DBSession,
    project: Project,
    name: str,
) -> ProjectEnvironmentVariable | None:
    return (
        dbsession.query(ProjectEnvironmentVariable)
        .filter(ProjectEnvironmentVariable.project == project)
        .filter(ProjectEnvironmentVariable.name == name)
        .first()
    )


def get_env_variables_for_project(
    dbsession: DBSession, project: Project
) -> list[ProjectEnvironmentVariable]:
    return (
        dbsession.query(ProjectEnvironmentVariable)
        .filter(ProjectEnvironmentVariable.project == project)
        .order_by(ProjectEnvironmentVariable.name)
        .all()
    )


def set_env_variables(
    dbsession: DBSession,
    project: Project,
    env_variables: list[tuple[str, str]],
    by_api_key: ApiKey,
) -> Deployment | None:
    for name, value in env_variables:
        existed = False
        for env_variable in project.env_variables:
            if env_variable.name == name:
                existed = True
                env_variable.value = value
                env_variable.by_api_key = by_api_key
        if not existed:
            env_variable = ProjectEnvironmentVariable(
                id=uuid.uuid4().hex,
                name=name,
                value=value,
                project=project,
                by_api_key=by_api_key,
            )
            dbsession.add(env_variable)
    deployment = create_deployment(
        dbsession=dbsession,
        project=project,
        pull=False,
        by_api_key=by_api_key,
    )
    return deployment


def delete_env_variable(
    dbsession: DBSession,
    env_variable: ProjectEnvironmentVariable,
    by_api_key: ApiKey,
) -> Deployment | None:
    dbsession.delete(env_variable)
    deployment = create_deployment(
        dbsession=dbsession,
        project=env_variable.project,
        pull=False,
        by_api_key=by_api_key,
    )
    return deployment
