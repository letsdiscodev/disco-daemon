from cornice import Service

from disco.http.contexts.publishedports import SingleContext
from disco.utils.publishedports import remove_published_port

published_port_service = Service(
    name="published_port_service",
    path="/projects/{project_name}/published-ports/{host_port}",
    http_cache=(None, dict(private=True)),
    factory=SingleContext,
)


@published_port_service.delete(
    permission="remove_published_port",
)
def published_port_service_delete(request):
    deployment = remove_published_port(
        dbsession=request.dbsession,
        published_port=request.context.published_port,
        by_api_key=request.api_key,
    )
    return dict(
        deployment=dict(
            number=deployment.number,
        )
        if deployment is not None
        else None,
    )
