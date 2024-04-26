import logging

from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, Project, ProjectKeyValue
from disco.utils.encryption import decrypt, encrypt

log = logging.getLogger(__name__)


def get_value(dbsession: DBSession, project: Project, key: str) -> str | None:
    key_value = dbsession.query(ProjectKeyValue).get(
        {"key": key, "project_id": project.id}
    )
    if key_value is None:
        return None
    return decrypt(key_value.value)


def get_all_key_values_for_project(
    dbsession: DBSession, project: Project
) -> list[ProjectKeyValue]:
    return (
        dbsession.query(ProjectKeyValue)
        .filter(ProjectKeyValue.project == project)
        .all()
    )


def set_value(
    dbsession: DBSession,
    project: Project,
    key: str,
    value: str | None,
    by_api_key: ApiKey,
) -> None:
    log.info(
        "Project key value set %s (%s) by %s", key, project.log(), by_api_key.log()
    )
    key_value = dbsession.query(ProjectKeyValue).get(
        {"key": key, "project_id": project.id}
    )
    if key_value is not None:
        key_value.value = encrypt(value)
    else:
        key_value = ProjectKeyValue(
            project=project,
            key=key,
            value=encrypt(value),
        )
        dbsession.add(key_value)


def delete_value(
    dbsession: DBSession, project: Project, key: str, by_api_key: ApiKey
) -> None:
    key_value = dbsession.query(ProjectKeyValue).get(
        {"key": key, "project_id": project.id}
    )
    if key_value is not None:
        log.info(
            "Project key value deleted %s (%s) by %s",
            key,
            project.log(),
            by_api_key.log(),
        )
        dbsession.delete(key_value)
