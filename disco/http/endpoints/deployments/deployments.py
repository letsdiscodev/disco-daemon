from cornice import Service
from disco.utils.deployments import create_deployment
from disco.http.contexts.deployments import ListContext


deployments_service = Service(
    name="deployments_service",
    path="/projects/{project_name}/deployments",
    http_cache=(None, dict(private=True)),
    factory=ListContext,
)


@deployments_service.post(
    permission="create_deployment",
)
def deployments_service_post(request):
    deployment = create_deployment(
        dbsession=request.dbsession,
        project=request.context.project,
        by_api_key=request.api_key,
    )
    request.response.status_code = 201
    return dict(
        deployment=dict(
            number=deployment.number,
        ),
    )
