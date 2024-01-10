from cornice import Service

from disco.http.contexts.envvariables import ListContext
from disco.http.validation.common import bound_schema_validator
from disco.http.validation.envvariables import SetEnvVariablesSchema
from disco.utils.envvariables import set_env_variables

env_variables_service = Service(
    name="env_variables_service",
    path="/projects/{project_name}/env",
    http_cache=(None, dict(private=True)),
    factory=ListContext,
)


@env_variables_service.get(
    permission="get_env_variables",
)
def env_variables_service_get(request):
    return dict(
        envVariables=[
            dict(
                name=env_variable.name,
                value=env_variable.value,
            )
            for env_variable in request.context.env_variables
        ]
    )


@env_variables_service.post(
    schema=SetEnvVariablesSchema(),
    validators=(bound_schema_validator,),
    permission="set_env_variables",
)
def env_variables_service_post(request):
    deployment = set_env_variables(
        dbsession=request.dbsession,
        project=request.context.project,
        env_variables=[
            (env_var["name"], env_var["value"])
            for env_var in request.validated["envVariables"]
        ],
        by_api_key=request.api_key,
    )
    return dict(
        deployment=dict(
            number=deployment.number,
        )
        if deployment is not None
        else None,
    )
