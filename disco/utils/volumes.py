import uuid

from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, Deployment, Project, ProjectVolume
from disco.utils.deployments import create_deployment


def attach_volume(
    dbsession: DBSession,
    project: Project,
    volume: str,
    destination: str,
    by_api_key: ApiKey,
) -> Deployment | None:
    # TODO if the volume is already attached, do nothing
    project_volume = ProjectVolume(
        id=uuid.uuid4().hex,
        project=project,
        name=volume,
        destination=destination,
        by_api_key=by_api_key,
    )
    dbsession.add(project_volume)
    return create_deployment(
        dbsession=dbsession,
        project=project,
        pull=False,
        image=None,
        by_api_key=by_api_key,
    )


def detach_volume(
    dbsession: DBSession, project: Project, volume: str, by_api_key: ApiKey
) -> Deployment | None:
    dbsession.query(ProjectVolume).filter(ProjectVolume.project == project).filter(
        ProjectVolume.volume == volume
    ).delete(synchronize_session=False)
    return create_deployment(
        dbsession=dbsession,
        project=project,
        pull=False,
        image=None,
        by_api_key=by_api_key,
    )
