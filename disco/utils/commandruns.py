import asyncio
import collections
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from secrets import token_hex
from typing import AsyncGenerator, Awaitable, Callable, Deque

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as AsyncDBSession
from sqlalchemy.orm.session import Session as DBSession

from disco.models import ApiKey, CommandRun, Deployment, Project
from disco.utils import commandoutputs, docker, keyvalues
from disco.utils.discofile import DiscoFile, get_disco_file_from_str
from disco.utils.encryption import decrypt
from disco.utils.projects import volume_name_for_project

log = logging.getLogger(__name__)


class AsyncBytesBuffer:
    MAX_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB

    def __init__(self):
        self._deque: Deque[bytes] = collections.deque()
        self._current_size = 0
        self._new_data_event = asyncio.Event()
        self._lock = asyncio.Lock()

    async def add_bytes(self, data: bytes):
        async with self._lock:
            data_len = len(data)
            if data_len > self.MAX_SIZE_BYTES:
                log.warning(
                    "Dropping %s bytes, larger than buffer %s bytes",
                    data_len,
                    self.MAX_SIZE_BYTES,
                )
                return
            while self._current_size + data_len > self.MAX_SIZE_BYTES and self._deque:
                oldest_data = self._deque.popleft()
                self._current_size -= len(oldest_data)
                log.warning("Buffer full, dropping %s bytes", len(oldest_data))
            self._deque.append(data)
            self._current_size += data_len
            self._new_data_event.set()  # Signal that new data is available

    async def requeue_front(self, data: bytes):
        async with self._lock:
            self._deque.appendleft(data)
            self._current_size += len(data)
            self._new_data_event.set()  # Signal that data is available

    async def stream(self) -> AsyncGenerator[bytes, None]:
        while True:
            async with self._lock:
                if self._deque:
                    chunk = self._deque.popleft()
                    self._current_size -= len(chunk)
                    if not self._deque:
                        self._new_data_event.clear()
                    yield chunk
                    # go back to the top of the loop to check for more data
                    continue
            # the deque was empty, wait for the event.
            await self._new_data_event.wait()


@dataclass
class Run:
    name: str
    stdin: AsyncBytesBuffer
    stdout: AsyncBytesBuffer
    stderr: AsyncBytesBuffer
    process: asyncio.Future[asyncio.subprocess.Process]
    last_interaction: datetime


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
        name=name,
        stdin=AsyncBytesBuffer(),
        stdout=AsyncBytesBuffer(),
        stderr=AsyncBytesBuffer(),
        last_interaction=datetime.now(timezone.utc),
        process=asyncio.get_running_loop().create_future(),
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
            args = [
                "docker",
                "container",
                "start",
                "--attach",
                "--interactive",
                name,
            ]
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
            )
            runs[run_id].process.set_result(process)

            async def write_stdin() -> None:
                assert process.stdin is not None
                async for chunk in runs[run_id].stdin.stream():
                    process.stdin.write(chunk)

            async def read_stdout() -> None:
                assert process.stdout is not None

                while True:
                    chunk = await process.stdout.read(1024)
                    if not chunk:
                        return
                    await runs[run_id].stdout.add_bytes(chunk)

            async def read_stderr() -> None:
                assert process.stderr is not None

                while True:
                    chunk = await process.stderr.read(1024)
                    if not chunk:
                        return
                    await runs[run_id].stderr.add_bytes(chunk)

            tasks = [
                asyncio.create_task(write_stdin()),
                asyncio.create_task(read_stdout()),
                asyncio.create_task(read_stderr()),
            ]
            await asyncio.gather(*tasks)
            # TODO del runs[run_id]
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
