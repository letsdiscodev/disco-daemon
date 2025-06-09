from dataclasses import dataclass
import logging
from secrets import token_hex
from typing import Awaitable, Callable, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, CommandRun, Deployment, Project
from disco.utils import commandoutputs, docker, keyvalues
from disco.utils.discofile import DiscoFile, get_disco_file_from_str
from disco.utils.encryption import decrypt
from disco.utils.projects import volume_name_for_project

log = logging.getLogger(__name__)


@dataclass
class Run:
    number: int
    name: str
    state: Literal['DECLARED', 'CREATED', 'STARTED', 'REMOVED']

runs: dict[str, Run] = {}


async def create_command_run(
    dbsession: AsyncDBSession,
    project: Project,
    deployment: Deployment,
    service: str,
    command: str,
    timeout: int,
    interactive: bool,
    by_api_key: ApiKey,
) -> tuple[CommandRun, Callable[[], Awaitable[None]]]:
    disco_file: DiscoFile = get_disco_file_from_str(deployment.disco_file)
    assert deployment.status == "COMPLETE"
    assert service in disco_file.services
    number = await get_next_run_number(dbsession, project)
    command_run = CommandRun(
        id=token_hex(16),
        number=number,
        service=service,
        command=command,
        status="CREATED",
        project=project,
        deployment=deployment,
        by_api_key=by_api_key,
    )
    dbsession.add(command_run)
    registry_host = await keyvalues.get_value(dbsession, "REGISTRY_HOST")
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
    depl_env_vars = await deployment.awaitable_attrs.env_variables
    env_variables = [
        (env_var.name, decrypt(env_var.value)) for env_var in depl_env_vars
    ]
    env_variables += [
        ("DISCO_PROJECT_NAME", project_name),
        ("DISCO_SERVICE_NAME", service),
        ("DISCO_HOST", await keyvalues.get_value_str(dbsession, "DISCO_HOST")),
        ("DISCO_DEPLOYMENT_NUMBER", str(deployment.number)),
    ]
    if deployment.commit_hash is not None:
        env_variables += [
            ("DISCO_COMMIT", deployment.commit_hash),
        ]

    network = docker.deployment_network_name(project.name, deployment.number)
    volumes = [
        ("volume", volume_name_for_project(v.name, project.id), v.destination_path)
        for v in disco_file.services[service].volumes
    ]

    name = f"{project_name}-run.{run_number}"
    run_id = command_run.id
    runs[run_id] = Run(
        number=number,
        name=name,
        state='DECLARED'
    )
    async def non_interactive_func() -> None:
        await commandoutputs.init(commandoutputs.run_source(run_id))

        async def log_output(output: str) -> None:
            await commandoutputs.store_output(commandoutputs.run_source(run_id), output)

        async def log_output_terminate():
            await commandoutputs.terminate(commandoutputs.run_source(run_id))

        stdout = log_output
        stderr = log_output
        try:
            await docker.run(
                image=image,
                project_name=project_name,
                name=name,
                env_variables=env_variables,
                volumes=volumes,
                networks=[network, "disco-main"],
                command=command,
                timeout=timeout,
                stdout=stdout,
                stderr=stderr,
            )
        except TimeoutError:
            await log_output(f"Timed out after {timeout} seconds\n")
        except docker.CommandRunProcessStatusError as ex:
            await log_output(f"Exited with code {ex.status}\n")
        except Exception:
            log.exception("Error when running command %s (%s)", command, name)
            await log_output("Internal Disco error\n")
        finally:
            await log_output_terminate()

    async def interactive_func() -> None:
        try:
            await docker.create_container(
                image=image,
                project_name=project_name,
                name=name,
                env_variables=env_variables,
                volumes=volumes,
                networks=[network, "disco-main"],
                command=command,
                timeout=timeout,
                interactive=True,
                tty=True,
            )
            runs[run_id].state= 'CREATED'

        except TimeoutError:
            log.exception("Timeout error")
            await docker.remove_container(name)
            # TODO f"Timed out after {timeout} seconds\n"
        except docker.CommandRunProcessStatusError:
            log.exception("docker.CommandRunProcessStatusError")
            await docker.remove_container(name)
            # TODO f"Exited with code {ex.status}\n" => should yield a status code for the CLI
        except Exception:
            log.exception("Error when running command %s (%s)", command, name)
            await docker.remove_container(name)
            # TODO  "Internal Disco error\n"

    if interactive:
        func = interactive_func
    else:
        func = non_interactive_func

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


async def get_command_run_by_id(
    dbsession: AsyncDBSession, run_id: str
) -> CommandRun | None:
    return await dbsession.get(CommandRun, run_id)


async def get_next_run_number(dbsession: AsyncDBSession, project: Project) -> int:
    stmt = (
        select(CommandRun)
        .where(CommandRun.project == project)
        .order_by(CommandRun.number.desc())
        .limit(1)
    )
    result = await dbsession.execute(stmt)
    run = result.scalar()
    if run is None:
        number = 0
    else:
        number = run.number
    return number + 1
