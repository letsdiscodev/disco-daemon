from cornice import Service
from pyramid.httpexceptions import HTTPNoContent

from disco.http.contexts.volumes import SingleContext
from disco.utils.docker import delete_volume

volume_service = Service(
    name="volume_service",
    path="/volumes/{volume_name}",
    http_cache=(None, dict(private=True)),
    factory=SingleContext,
)


@volume_service.delete(
    permission="delete_volume",
)
def volume_service_delete(request):
    delete_volume(
        name=request.matchdict["volume_name"],
        by_api_key=request.api_key,
    )
    return HTTPNoContent()
