from cornice import Service

from disco.http.contexts.deployments import ListContext
from disco.http.validation.common import bound_schema_validator
from disco.http.validation.deployments import CreateDeploymentSchema
from disco.utils.deployments import create_deployment

deployments_service = Service(
    name="deployments_service",
    path="/projects/{project_name}/deployments",
    http_cache=(None, dict(private=True)),
    factory=ListContext,
)


@deployments_service.post(
    schema=CreateDeploymentSchema(),
    validators=(bound_schema_validator,),
    permission="create_deployment",
)
def deployments_service_post(request):
    deployment = create_deployment(
        dbsession=request.dbsession,
        project=request.context.project,
        commit_hash=request.validated["commit"],
        disco_config=request.validated["discoConfig"],
        by_api_key=request.api_key,
    )
    request.response.status_code = 201
    return dict(
        deployment=dict(
            number=deployment.number,
        ),
    )
