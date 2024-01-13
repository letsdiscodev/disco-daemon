import logging

log = logging.getLogger(__name__)


def process_github_webhook(with_dbsession, task_body):
    from disco.utils.deployments import create_deployment
    from disco.utils.github import get_branch_for_webhook_push
    from disco.utils.projects import get_project_by_id

    project_id = task_body["project_id"]
    request_body = task_body["request_body"]
    log.info("Processing Github Webhook %s", request_body)
    branch = get_branch_for_webhook_push(request_body)
    if branch not in ["master", "main"]:
        return

    def start_github_build(dbsession):
        project = get_project_by_id(dbsession, project_id)
        if project is None:
            raise Exception(f"Project {project_id} not found")
        create_deployment(
            dbsession=dbsession, project=project, pull=True, image=None, by_api_key=None
        )

    with_dbsession(start_github_build)


def process_deployment(with_dbsession, task_body):
    from disco.utils.deployments import (
        BUILD_STATUS,
        build,
        get_deployment_by_id,
        set_deployment_status,
    )

    db_data = dict()
    deployment_id = task_body["deployment_id"]

    def _set_deployment_status(status: BUILD_STATUS) -> None:
        def inner(dbsession):
            deployment = get_deployment_by_id(dbsession, deployment_id)
            if deployment is None:
                raise Exception(f"Deployment {deployment_id} not found")
            set_deployment_status(deployment, status)

        with_dbsession(inner)

    _set_deployment_status("STARTED")

    def get_db_data(dbsession):
        deployment = get_deployment_by_id(dbsession, deployment_id)
        if deployment is None:
            raise Exception(f"Deployment {deployment_id} not found")
        db_data["project_name"] = deployment.project.name
        db_data["project_domain"] = deployment.project.domain
        db_data["github_repo"] = deployment.project.github_repo
        db_data["github_host"] = deployment.project.github_host
        db_data["deployment_number"] = deployment.number
        db_data["pull"] = deployment.pull
        db_data["image"] = deployment.image
        db_data["env_variables"] = [
            (env_var.name, env_var.value) for env_var in deployment.env_variables
        ]
        db_data["volumes"] = [
            (volume.name, volume.destination) for volume in deployment.volumes
        ]
        db_data["published_ports"] = [
            (published_port.host_port, published_port.container_port)
            for published_port in deployment.published_ports
        ]
        db_data["exposed_ports"] = (
            [8000] if db_data["project_domain"] is not None else []
        )

    with_dbsession(get_db_data)

    build(
        project_name=db_data["project_name"],
        project_domain=db_data["project_domain"],
        github_repo=db_data["github_repo"],
        github_host=db_data["github_host"],
        deployment_number=db_data["deployment_number"],
        pull=db_data["pull"],
        image=db_data["image"],
        env_variables=db_data["env_variables"],
        volumes=db_data["volumes"],
        set_deployment_status=_set_deployment_status,
        published_ports=db_data["published_ports"],
        exposed_ports=db_data["exposed_ports"],
    )


HANDLERS = dict(
    PROCESS_GITHUB_WEBHOOK=process_github_webhook,
    PROCESS_DEPLOYMENT=process_deployment,
)
