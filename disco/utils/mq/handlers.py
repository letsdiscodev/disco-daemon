import json
import logging
from typing import Any

from disco.models.db import Session
from disco.utils import commandoutputs

log = logging.getLogger(__name__)


def process_github_webhook(task_body):
    from disco.utils.deployments import create_deployment
    from disco.utils.github import get_commit_info_from_webhook_push
    from disco.utils.projects import get_project_by_id

    project_id = task_body["project_id"]
    request_body = task_body["request_body"]
    log.info("Processing Github Webhook for project %s", project_id)
    log.info("Processing Github Webhook %s", request_body)
    branch, commit_hash = get_commit_info_from_webhook_push(request_body)
    if branch not in ["master", "main"]:
        log.info("Branch was not master or main, skipping")
        return

    with Session() as dbsession:
        with dbsession.begin():
            project = get_project_by_id(dbsession, project_id)
            if project is None:
                raise Exception(f"Project {project_id} not found")
            create_deployment(
                dbsession=dbsession,
                project=project,
                commit_hash=commit_hash,
                disco_config=None,
                by_api_key=None,
            )


def process_deployment(task_body):
    from disco.utils import caddy, docker, github, keyvalues
    from disco.utils.deployments import (
        BUILD_STATUS,
        get_deployment_by_id,
        get_previous_deployment,
        set_deployment_status,
    )
    from disco.utils.filesystem import project_folder_exists, read_disco_file

    db_data = dict()
    deployment_id = task_body["deployment_id"]

    def log_output(output: str) -> None:
        with Session() as dbsession:
            with dbsession.begin():
                commandoutputs.save(dbsession, f"DEPLOYMENT_{deployment_id}", output)

    try:
        log_output(f"Starting deployment ID {deployment_id}\n")

        def _set_deployment_status(status: BUILD_STATUS) -> None:
            with Session() as dbsession:
                with dbsession.begin():
                    deployment = get_deployment_by_id(dbsession, deployment_id)
                    if deployment is None:
                        raise Exception(f"Deployment {deployment_id} not found")
                    set_deployment_status(deployment, status)

        _set_deployment_status("STARTED")

        with Session() as dbsession:
            with dbsession.begin():
                log.info("Getting data from database for deployment %s", deployment_id)
                deployment = get_deployment_by_id(dbsession, deployment_id)
                if deployment is None:
                    raise Exception(f"Deployment {deployment_id} not found")
                prev_deployment = get_previous_deployment(dbsession, deployment)
                db_data["project_id"] = deployment.project.id
                db_data["project_domain"] = deployment.project.domain
                db_data["project_name"] = deployment.project.name
                db_data["github_repo"] = deployment.project.github_repo
                db_data["github_host"] = deployment.project.github_host
                db_data["deployment_number"] = deployment.number
                db_data["commit_hash"] = deployment.commit_hash
                db_data["disco_config_str"] = deployment.disco_config
                db_data["env_variables"] = [
                    (env_var.name, env_var.value)
                    for env_var in deployment.env_variables
                ]
                db_data["disco_domain"] = keyvalues.get_value(dbsession, "DISCO_DOMAIN")
                db_data["prev_project_name"] = (
                    prev_deployment.project_name
                    if prev_deployment is not None
                    else None
                )
                db_data["prev_disco_config_str"] = (
                    prev_deployment.disco_config
                    if prev_deployment is not None
                    else None
                )

        log_output(
            f"Deployment number {db_data['deployment_number']} of {db_data['project_name']}\n"
        )

        if db_data["commit_hash"] is not None:
            _set_deployment_status("PULLING")
            log_output(f"Deployment of git {db_data['commit_hash']}\n")
            if not project_folder_exists(db_data["project_id"]):
                log_output(f"Cloning project from {db_data['github_repo']}\n")
                github.clone_project(
                    project_id=db_data["project_id"],
                    github_repo=db_data["github_repo"],
                    github_host=db_data["github_host"],
                    log_output=log_output,
                )
            else:
                log_output("Fetching latest commits from git repo\n")
                github.fetch(project_id=db_data["project_id"], log_output=log_output)
            github.checkout_commit(
                db_data["project_id"], db_data["commit_hash"], log_output=log_output
            )

        if db_data["disco_config_str"] is None:
            log_output("Reading Disco config from project folder\n")
            disco_config_str = read_disco_file(db_data["project_id"])
            if disco_config_str is not None:
                log_output("Found disco.json\n")

                db_data["disco_config_str"] = disco_config_str

                with Session() as dbsession:
                    with dbsession.begin():
                        deployment = get_deployment_by_id(dbsession, deployment_id)
                        deployment.disco_config = disco_config_str

        if db_data["disco_config_str"] is None:
            log_output("No disco.json found, falling back to default config\n")
            default_config = dict(
                version="1.0",
                services=dict(
                    web=dict(
                        image=dict(
                            dockerfile="Dockerfile",
                            context=".",
                        ),
                        port=8000,
                        command=None,
                    )
                ),
            )
            db_data["disco_config_str"] = json.dumps(default_config)
        log.info("Decoding JSON config for deployment %s", deployment_id)
        try:
            config = json.loads(db_data["disco_config_str"])
        except Exception:
            log_output("Failed to decode JSON of disco.json\n")
            raise
        if "services" not in config:
            config["services"] = dict()
        prev_config = (
            json.loads(db_data["prev_disco_config_str"])
            if db_data["prev_disco_config_str"]
            else dict(services=dict())
        )
        # build images
        images = set()
        log_output("Building images\n")
        _set_deployment_status("BUILDING_IMAGES")
        for service_name, service in config["services"].items():
            if _pull(service) is not None:
                # Docker will take care of pulling when service is created
                continue
            image = docker.image_name(
                disco_domain=db_data["disco_domain"],
                project_id=db_data["project_id"],
                deployment_number=db_data["deployment_number"],
                dockerfile=_dockerfile(service),
                context=_context(service),
            )
            if image not in images:
                images.add(image)
                log_output(f"Building image of {service_name}\n")
                docker.build_image(
                    image=image,
                    project_id=db_data["project_id"],
                    dockerfile=_dockerfile(service),
                    context=_context(service),
                    log_output=log_output,
                )
        _set_deployment_status("PUSHING_IMAGES")
        log_output("Pushing images to Disco registry\n")
        for image in images:
            log.info("Pushing image %s deployment %s", image, deployment_id)
            docker.push_image(image, log_output=log_output)

        # start new services
        web_is_started = False
        log_output("Starting/stopping services\n")
        _set_deployment_status("STARTING")
        for service_name, service in config["services"].items():
            if len(service.get("publishedPorts", [])) > 0:
                # do not start new service yet
                # to avoid conflicts with previous service
                # TODO optimize: verify if previous deployment
                #                also had the same published port
                log.info(
                    "Not starting service %s (published ports, need to wait for "
                    "previous service to be removed) for deployment %s",
                    service_name,
                    deployment_id,
                )
                continue
            internal_service_name = docker.service_name(
                db_data["project_name"], service_name, db_data["deployment_number"]
            )
            if _pull(service) is not None:
                image = _pull(service)
            else:
                image = docker.image_name(
                    disco_domain=db_data["disco_domain"],
                    project_id=db_data["project_id"],
                    deployment_number=db_data["deployment_number"],
                    dockerfile=_dockerfile(service),
                    context=_context(service),
                )
            log_output(f"Starting service {service_name}\n")
            docker.start_service(
                image=image,
                name=internal_service_name,
                project_name=db_data["project_name"],
                project_service_name=service_name,
                env_variables=db_data["env_variables"],
                volumes=[
                    (v["name"], v["destinationPath"])
                    for v in service.get("volumes", [])
                ],
                published_ports=[
                    (p["publishedAs"], p["fromContainerPort"], p["protocol"])
                    for p in service.get("publishedPorts", [])
                ],
                command=service.get("command"),
                log_output=log_output,
            )
            if service_name == "web":
                web_is_started = True
        if db_data["project_domain"] is not None and web_is_started:
            internal_service_name = docker.service_name(
                db_data["project_name"], "web", db_data["deployment_number"]
            )
            # TODO wait that it's listening on the port specified?
            log_output("Sending traffic to new web service\n")
            caddy.serve_service(
                db_data["project_id"],
                db_data["project_domain"],
                internal_service_name,
                port=_port(config["services"]["web"]),
            )
        _set_deployment_status("STOPPING_OLD")
        if db_data["deployment_number"] > 1:
            _set_deployment_status("CLEAN_UP")
            for service_name in prev_config["services"]:
                internal_service_name = docker.service_name(
                    db_data["prev_project_name"],
                    service_name,
                    db_data["deployment_number"] - 1,
                )
                log_output(f"Stopping previous service {service_name}\n")
                docker.stop_service(internal_service_name, log_output=log_output)
        web_is_now_started = False
        log_output("Starting services with published ports\n")
        _set_deployment_status("STARTING_PUBLISHED_PORTS")
        for service_name, service in config["services"].items():
            if len(service.get("publishedPorts", [])) == 0:
                # already started above, skip
                continue
            internal_service_name = docker.service_name(
                db_data["project_name"], service_name, db_data["deployment_number"]
            )
            if _pull(service) is not None:
                image = _pull(service)
            else:
                image = docker.image_name(
                    disco_domain=db_data["disco_domain"],
                    project_id=db_data["project_id"],
                    deployment_number=db_data["deployment_number"],
                    dockerfile=_dockerfile(service),
                    context=_context(service),
                )
            log_output(f"Starting services {service_name} with published ports\n")
            docker.start_service(
                image=image,
                name=internal_service_name,
                project_name=db_data["project_name"],
                project_service_name=service_name,
                env_variables=db_data["env_variables"],
                volumes=[
                    (v["name"], v["destinationPath"])
                    for v in service.get("volumes", [])
                ],
                published_ports=[
                    (p["publishedAs"], p["fromContainerPort"], p["protocol"])
                    for p in service.get("publishedPorts", [])
                ],
                command=service.get("command"),
                log_output=log_output,
            )
            if service_name == "web":
                web_is_now_started = True
        if db_data["project_domain"] is not None and web_is_now_started:
            internal_service_name = docker.service_name(
                db_data["project_name"], "web", db_data["deployment_number"]
            )
            # TODO wait that it's listening on the port specified?
            log_output("Sending traffic to new web service\n")
            caddy.serve_service(
                db_data["project_id"],
                db_data["project_domain"],
                internal_service_name,
                port=_port(config["services"]["web"]),
            )
        _set_deployment_status("DONE")
        log_output("Deployment complete\n")
    finally:
        log_output(None)  # end


def set_syslog_service(task_body):
    from disco.utils import docker, keyvalues, syslog

    db_data = dict()

    with Session() as dbsession:
        with dbsession.begin():
            db_data["disco_domain"] = keyvalues.get_value(dbsession, "DISCO_DOMAIN")
            db_data["urls"] = syslog.get_syslog_urls(dbsession)

    docker.set_syslog_service(db_data["disco_domain"], db_data["urls"])


HANDLERS = dict(
    PROCESS_GITHUB_WEBHOOK=process_github_webhook,
    PROCESS_DEPLOYMENT=process_deployment,
    SET_SYSLOG_SERVICE=set_syslog_service,
)


def _dockerfile(service: dict[str, Any]) -> str | None:
    default = "Dockerfile"
    if "image" not in service:
        return default
    if "dockerfile" not in service["image"]:
        return default
    return service["image"]["dockerfile"]


def _context(service: dict[str, Any]) -> str | None:
    default = "."
    if "image" not in service:
        return default
    if "context" not in service["image"]:
        return default
    return service["image"]["context"]


def _pull(service: dict[str, Any]) -> str | None:
    default = None
    if "image" not in service:
        return default
    if "pull" not in service["image"]:
        return default
    return service["image"]["pull"]


def _port(service: dict[str, Any]) -> int:
    default = 8000
    if "port" not in service:
        return default
    return service["port"]
