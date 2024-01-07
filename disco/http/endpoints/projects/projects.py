from cornice import Service
from disco.http.validation.common import bound_schema_validator
from disco.http.validation.projects import CreateProjectSchema
from disco.utils.projects import create_project
from disco.http.contexts.projects import ListContext


projects_service = Service(
    name="projects_service",
    path="/projects",
    http_cache=(None, dict(private=True)),
    factory=ListContext,
)


@projects_service.get(
    permission="get_projects",
)
def projects_service_get(request):
    return dict(
        projects=[
            dict(
                id=project.id,
                name=project.name,
                githubRepo=project.github_repo,
            )
            for project in request.context.projects
        ],
    )


@projects_service.post(
    schema=CreateProjectSchema(),
    validators=(bound_schema_validator,),
    permission="create_project",
)
def projects_service_post(request):
    project = create_project(
        dbsession=request.dbsession,
        name=request.validated["name"],
        github_repo=request.validated["githubRepo"],
    )
    return dict(
        project=dict(
            id=project.id,
            name=project.name,
            githubRepo=project.github_repo,
        ),
    )
