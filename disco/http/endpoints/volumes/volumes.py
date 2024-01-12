from cornice import Service
from pyramid.httpexceptions import HTTPCreated

from disco.http.contexts.volumes import ListContext
from disco.http.validation.common import bound_schema_validator
from disco.http.validation.volumes import CreateVolumeSchema
from disco.utils.docker import create_volume

volumes_service = Service(
    name="volumes_service",
    path="/volumes",
    http_cache=(None, dict(private=True)),
    factory=ListContext,
)


@volumes_service.get(
    permission="get_volumes",
)
def volumes_service_get(request):
    return dict(
        volumes=[
            dict(
                name=volume_name,
            )
            for volume_name in request.context.volume_names
        ]
    )


@volumes_service.post(
    schema=CreateVolumeSchema(),
    validators=(bound_schema_validator,),
    permission="create_volume",
)
def volumes_service_post(request):
    create_volume(
        name=request.validated["name"],
        by_api_key=request.api_key,
    )
    return HTTPCreated()
