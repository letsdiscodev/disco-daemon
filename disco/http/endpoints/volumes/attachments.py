from cornice import Service

from disco.http.contexts.volumes import SingleContext
from disco.http.validation.common import bound_schema_validator
from disco.http.validation.volumes import AttachVolumeSchema
from disco.utils.projects import get_project_by_name
from disco.utils.volumes import attach_volume, detach_volume

volume_attachments_service = Service(
    name="volume_attachments_service",
    path="/volumes/{volume_name}/attachments/{project_name}",
    http_cache=(None, dict(private=True)),
    factory=SingleContext,
)


@volume_attachments_service.post(
    schema=AttachVolumeSchema(),
    validators=(bound_schema_validator,),
    permission="attach_volume",
)
def volume_attachments_service_post(request):
    # TODO create other context and move project to context
    project = get_project_by_name(request.dbsession, request.matchdict["project_name"])
    deployment = attach_volume(
        dbsession=request.dbsession,
        project=project,
        volume=request.matchdict["volume_name"],
        destination=request.validated["destination"],
        by_api_key=request.api_key,
    )
    return dict(
        deployment=dict(
            number=deployment.number,
        )
        if deployment is not None
        else None,
    )


@volume_attachments_service.delete(
    permission="detach_volume",
)
def volume_attachments_service_delete(request):
    deployment = detach_volume(
        dbsession=request.dbsession,
        volume=request.matchdict["volume_name"],
        project=request.context.project,
        by_api_key=request.api_key,
    )
    return dict(
        deployment=dict(
            number=deployment.number,
        )
        if deployment is not None
        else None,
    )
