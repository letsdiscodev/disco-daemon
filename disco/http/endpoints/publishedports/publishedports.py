from cornice import Service

from disco.http.contexts.publishedports import ListContext
from disco.http.validation.common import bound_schema_validator
from disco.http.validation.publishedports import AddPublishedPortSchema
from disco.utils.publishedports import add_published_port

published_ports_service = Service(
    name="published_ports_service",
    path="/projects/{project_name}/published-ports",
    http_cache=(None, dict(private=True)),
    factory=ListContext,
)


@published_ports_service.get(
    permission="get_published_ports",
)
def published_ports_service_get(request):
    return dict(
        publishedPorts=[
            dict(
                hostPort=published_port.host_port,
                containerPort=published_port.container_port,
            )
            for published_port in request.context.project.published_ports
        ]
    )


@published_ports_service.post(
    schema=AddPublishedPortSchema(),
    validators=(bound_schema_validator,),
    permission="add_published_ports",
)
def published_ports_service_post(request):
    deployment = add_published_port(
        dbsession=request.dbsession,
        project=request.context.project,
        host_port=request.validated["hostPort"],
        container_port=request.validated["containerPort"],
        by_api_key=request.api_key,
    )
    request.response.status_code = 201
    return dict(
        deployment=dict(
            number=deployment.number,
        )
        if deployment is not None
        else None,
    )
