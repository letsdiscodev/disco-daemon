import logging
import uuid
from typing import Callable

from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, CommandRun, Deployment, Project
from disco.models.db import Session
from disco.utils import commandoutputs, docker, keyvalues
from disco.utils.discofile import DiscoFile, ServiceType, get_disco_file_from_str
from disco.utils.encryption import decrypt

log = logging.getLogger(__name__)


def create_command_run(
    dbsession: DBSession,
    project: Project,
    deployment: Deployment,
    service: str,
    command: str,
    timeout: int,
    by_api_key: ApiKey,
) -> tuple[CommandRun, Callable[[], None]]:
    disco_file: DiscoFile = get_disco_file_from_str(deployment.disco_file)
    assert deployment.status == "COMPLETE"
    assert service in disco_file.services
    number = get_next_run_number(dbsession, project)
    command_run = CommandRun(
        id=uuid.uuid4().hex,
        number=number,
        service=service,
        command=command,
        status="CREATED",
        project=project,
        deployment=deployment,
        by_api_key=by_api_key,
    )
    dbsession.add(command_run)
    registry_host = keyvalues.get_value(dbsession, "REGISTRY_HOST")
    image = docker.get_image_name_for_service(
        disco_file=disco_file,
        service_name=service,
        registry_host=registry_host,
        project_name=project.name,
        deployment_number=deployment.number,
    )
    project_name = project.name
    run_number = command_run.number
    run_id = command_run.id
    if disco_file.services[service].type == ServiceType.command:
        command = f"{disco_file.services[service].command} {command}"
    env_variables = [
        (env_var.name, decrypt(env_var.value)) for env_var in deployment.env_variables
    ]
    env_variables += [
        ("DISCO_PROJECT_NAME", project_name),
        ("DISCO_SERVICE_NAME", service),
        ("DISCO_COMMIT", deployment.commit_hash),
        ("DISCO_HOST", keyvalues.get_value(dbsession, "DISCO_HOST")),
        ("DISCO_IP", keyvalues.get_value(dbsession, "DISCO_IP")),
        ("DISCO_API_KEY", by_api_key.id),
    ]
    if deployment.domain is not None:
        env_variables += [
            ("DISCO_PROJECT_DOMAIN", deployment.domain),
        ]
    network = docker.deployment_network_name(project.name, deployment.number)
    volumes = [
        ("volume", v.name, v.destination_path)
        for v in disco_file.services[service].volumes
    ]

    def func() -> None:
        def log_output(output: str | None) -> None:
            if output is not None:
                log.info("Command run %s %s: %s", project_name, run_number, output)
            with Session() as dbsession_:
                with dbsession_.begin():
                    commandoutputs.save(dbsession_, f"RUN_{run_id}", output)

        try:
            docker.run(
                image=image,
                project_name=project_name,
                name=f"{project_name}-run.{run_number}",
                env_variables=env_variables,
                volumes=volumes,
                networks=[network, "disco-caddy-daemon"],
                command=command,
                timeout=timeout,
                log_output=log_output,
            )
        except Exception:
            log_output("Failed")
        finally:
            log_output(None)

    return command_run, func


def get_command_run_by_number(
    dbsession: DBSession, project: Project, number: int
) -> CommandRun | None:
    return (
        dbsession.query(CommandRun)
        .filter(CommandRun.project == project)
        .filter(CommandRun.number == number)
        .first()
    )


def get_next_run_number(dbsession: DBSession, project: Project) -> int:
    run = (
        dbsession.query(CommandRun)
        .filter(CommandRun.project == project)
        .order_by(CommandRun.number.desc())
        .first()
    )
    if run is None:
        number = 0
    else:
        number = run.number
    return number + 1
