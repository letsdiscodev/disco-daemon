from cornice import Service

from disco.http.contexts.envvariables import SingleContext
from disco.utils.envvariables import delete_env_variable

env_variable_service = Service(
    name="env_variable_service",
    path="/projects/{project_name}/env/{env_var_name}",
    http_cache=(None, dict(private=True)),
    factory=SingleContext,
)


@env_variable_service.get(
    permission="get_env_variable",
)
def env_variable_service_get(request):
    return dict(
        envVariable=dict(
            name=request.context.env_variable.name,
            value=request.context.env_variable.value,
        )
    )


@env_variable_service.delete(
    permission="delete_env_variable",
)
def env_variable_service_delete(request):
    deployment = delete_env_variable(
        dbsession=request.dbsession,
        env_variable=request.context.env_variable,
        by_api_key=request.api_key,
    )
    return dict(
        deployment=dict(
            number=deployment.number,
        )
        if deployment is not None
        else None,
    )
