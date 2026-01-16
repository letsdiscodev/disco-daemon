import asyncio
import fcntl
import json
import logging
import os
import pty
import shlex
import struct
import termios
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Sequence

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError
from sqlalchemy.orm.session import Session as DBSession
from sse_starlette import ServerSentEvent
from sse_starlette.sse import EventSourceResponse

from disco.auth import get_api_key_sync, get_api_key_wo_tx, validate_token
from disco.endpoints.dependencies import get_db_sync, get_project_from_url_sync
from disco.models import ApiKey, Project
from disco.models.db import AsyncSession, Session
from disco.models.deploymentenvironmentvariable import DeploymentEnvironmentVariable
from disco.utils import commandoutputs, keyvalues
from disco.utils.commandruns import create_command_run, get_command_run_by_number
from disco.utils.deployments import get_live_deployment, get_live_deployment_sync
from disco.utils.discofile import DiscoFile, ServiceType, get_disco_file_from_str
from disco.utils.docker import (
    deployment_network_name,
    get_image_name_for_service,
    remove_container,
)
from disco.utils.encryption import decrypt
from disco.utils.projects import (
    get_project_by_name,
    get_project_by_name_sync,
    volume_name_for_project,
)

log = logging.getLogger(__name__)

router = APIRouter()


MAX_LIFETIME_SECONDS = 24 * 60 * 60
HEARTBEAT_INTERVAL_SECONDS = 30


@router.websocket("/api/projects/{project_name}/run")
async def run_ws(
    websocket: WebSocket,
    project_name: str,
):
    """Interactive shell session in a project's container.

    Protocol:
    1. Client connects
    2. Server accepts
    3. Client sends: {"token": "<bearer_jwt>"}
    4. Server sends: {"type": "connected", "container": "..."} on success
    5. After auth:
       - Binary frames = terminal I/O
       - Text frames = JSON control: {"type": "resize", "rows": 24, "cols": 80}

    """
    await websocket.accept()

    # ===== STEP 1: Authenticate =====
    # Expects: {"token": "...", "service": "optional-service-name"}
    try:
        msg = await asyncio.wait_for(websocket.receive_json(), timeout=10.0)
        if not isinstance(msg, dict):
            raise ValueError("JSON object expected")
    except Exception:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    token = msg.get("token")
    requested_service = msg.get("service")  # Optional
    command = msg.get("command")

    if not token:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    api_key = await validate_token(token)
    if api_key is None:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    if not command:
        await websocket.close(code=4023, reason="Command required")
        return

    # ===== STEP 2: Get project and deployment info =====
    async with AsyncSession.begin() as dbsession:
        project = await get_project_by_name(dbsession, project_name)
        if project is None:
            await websocket.close(code=4004, reason="Project not found")
            return

        deployment = await get_live_deployment(dbsession, project)
        if deployment is None:
            await websocket.close(code=4022, reason="No live deployment")
            return

        disco_file: DiscoFile = get_disco_file_from_str(deployment.disco_file)

        # Determine which service to use
        if requested_service:
            if requested_service not in disco_file.services:
                await websocket.close(
                    code=4022, reason=f"Service '{requested_service}' not found"
                )
                return
            if disco_file.services[requested_service].type == ServiceType.static:
                await websocket.close(
                    code=4022, reason=f"Service '{requested_service}' is static"
                )
                return
            service = requested_service
        else:
            service = get_default_service_for_run(disco_file)
            if service is None:
                await websocket.close(code=4022, reason="No service can run commands")
                return

        # Gather container config
        registry_host = await keyvalues.get_value(dbsession, "REGISTRY_HOST")
        image = get_image_name_for_service(
            disco_file=disco_file,
            service_name=service,
            registry_host=registry_host,
            project_name=project.name,
            deployment_number=deployment.number,
        )

        deployment_env_variables: Sequence[
            DeploymentEnvironmentVariable
        ] = await deployment.awaitable_attrs.env_variables
        
        env_variables = [
            (env_var.name, decrypt(env_var.value))
            for env_var in deployment_env_variables
        ]
        env_variables += [
            ("DISCO_PROJECT_NAME", project.name),
            ("DISCO_SERVICE_NAME", service),
            ("DISCO_HOST", await keyvalues.get_value_str(dbsession, "DISCO_HOST")),
            ("DISCO_DEPLOYMENT_NUMBER", str(deployment.number)),
        ]
        if deployment.commit_hash is not None:
            env_variables.append(("DISCO_COMMIT", deployment.commit_hash))

        network = deployment_network_name(project.name, deployment.number)
        volumes = [
            ("volume", volume_name_for_project(v.name, project.id), v.destination_path)
            for v in disco_file.services[service].volumes
        ]

        # Generate session ID and container name
        session_id = uuid.uuid4().hex
        container_name = f"{project.name}-run.{session_id[:8]}"
        created = int(time.time())
        expires = int(
            (
                datetime.now(timezone.utc) + timedelta(seconds=MAX_LIFETIME_SECONDS)
            ).timestamp()
        )

    # ===== STEP 3: Run container with PTY =====
    master_fd = None
    slave_fd = None
    proc = None

    try:
        # Create PTY pair
        master_fd, slave_fd = pty.openpty()

        # Set initial size (will be resized by client after connection)
        set_pty_size(master_fd, 24, 80)

        # Build docker run args - use --rm so container auto-removes on exit
        args = ["docker", "run"]
        args += ["--name", container_name]
        args += ["--rm"]  # Auto-remove on exit
        args += ["--restart=no"]  # Explicitly prevent restart

        # Labels for identification and cleanup
        args += ["--label", f"disco.project.name={project_name}"]
        args += ["--label", "disco.run=true"]  # Easy filter
        args += ["--label", f"disco.run.created={created}"]
        args += ["--label", f"disco.run.expires={expires}"]

        args += ["--interactive", "--tty"]
        args += ["--log-driver", "none"]  # do not include output in logs
        args += ["--network", network]
        args += ["--network", "disco-main"]

        for var_name, var_value in env_variables:
            args += ["--env", f"{var_name}={var_value}"]

        for volume_type, source, destination in volumes:
            args += [
                "--mount",
                f"type={volume_type},source={source},destination={destination}",
            ]

        args += [image]
        args += shlex.split(command)

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
        )
        os.close(slave_fd)  # Parent doesn't need slave
        slave_fd = None

        # Set master to non-blocking
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        await websocket.send_json({"type": "connected", "container": container_name})

        # ===== STEP 4: Bridge PTY <-> WebSocket =====
        await bridge_pty_websocket(websocket, master_fd, proc)

        # Send exit message with exit code (useful for command mode)
        exit_code = proc.returncode if proc.returncode is not None else 1
        try:
            await websocket.send_json({"type": "exit", "code": exit_code})
        except Exception:
            pass

        # Shell exited - close the WebSocket
        try:
            await websocket.close(code=1000, reason="Shell exited")
        except Exception:
            pass

    except WebSocketDisconnect:
        log.info("WebSocket disconnected for shell session %s", container_name)
    except Exception as e:
        log.exception("Error in shell session: %s", e)
        try:
            await websocket.close(code=4500, reason=str(e)[:100])
        except Exception:
            pass
    finally:
        if slave_fd is not None:
            try:
                os.close(slave_fd)
            except Exception:
                pass
        if master_fd is not None:
            try:
                os.close(master_fd)
            except Exception:
                pass
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except Exception:
                proc.kill()
        try:
            await remove_container(container_name)
        except Exception:
            pass


async def bridge_pty_websocket(
    websocket: WebSocket,
    master_fd: int,
    proc: asyncio.subprocess.Process,
):
    """Bridge between PTY file descriptor and WebSocket."""
    exit_event = asyncio.Event()

    async def watch_process():
        """Watch for process exit."""
        await proc.wait()
        exit_event.set()

    async def pty_to_websocket():
        """Read from PTY, send to WebSocket using event-driven I/O."""
        loop = asyncio.get_running_loop()
        data_ready = asyncio.Event()

        def on_readable():
            data_ready.set()

        loop.add_reader(master_fd, on_readable)
        try:
            while not exit_event.is_set():
                # Wait for data or timeout to check exit_event
                try:
                    await asyncio.wait_for(data_ready.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                data_ready.clear()

                # Read and send all available data
                try:
                    while True:
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        await websocket.send_bytes(data)
                except BlockingIOError:
                    pass  # No more data available
                except OSError:
                    break
        finally:
            loop.remove_reader(master_fd)

    async def websocket_to_pty():
        """Read from WebSocket, write to PTY."""
        while not exit_event.is_set():
            try:
                # Use wait_for to allow checking exit_event periodically
                message = await asyncio.wait_for(websocket.receive(), timeout=0.5)

                if message["type"] == "websocket.disconnect":
                    break

                if "bytes" in message:
                    os.write(master_fd, message["bytes"])
                elif "text" in message:
                    try:
                        ctrl = json.loads(message["text"])
                        if ctrl.get("type") == "resize":
                            log.info(
                                "Resize request: rows=%s, cols=%s",
                                ctrl["rows"],
                                ctrl["cols"],
                            )
                            set_pty_size(master_fd, ctrl["rows"], ctrl["cols"])
                        elif ctrl.get("type") == "pong":
                            log.info("Received pong from client")
                        # Ignore other control messages
                    except json.JSONDecodeError:
                        os.write(master_fd, message["text"].encode())

            except asyncio.TimeoutError:
                continue
            except WebSocketDisconnect:
                break

    async def heartbeat():
        """Send periodic ping to keep connection alive and detect dead clients."""
        while not exit_event.is_set():
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if not exit_event.is_set():
                    await websocket.send_json({"type": "ping"})
            except Exception:
                break

    # Start process watcher
    watch_task = asyncio.create_task(watch_process())

    _, pending = await asyncio.wait(
        [
            asyncio.create_task(pty_to_websocket()),
            asyncio.create_task(websocket_to_pty()),
            asyncio.create_task(heartbeat()),
            watch_task,
        ],
        return_when=asyncio.FIRST_COMPLETED,
    )

    exit_event.set()  # Signal all tasks to stop

    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def set_pty_size(fd: int, rows: int, cols: int):
    """Set the PTY window size."""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def get_default_service_for_run(disco_file: DiscoFile) -> str | None:
    """
    Determine which service to run shell in.
    Same logic as run.py - prefers 'web' service, falls back to first non-static.
    """
    if len(list(disco_file.services.keys())) == 0:
        return None

    if (
        "web" in disco_file.services
        and disco_file.services["web"].type != ServiceType.static
    ):
        return "web"

    for name, service in disco_file.services.items():
        if service.type != ServiceType.static:
            return name

    return None


class RunReqBody(BaseModel):
    command: str = Field(..., max_length=4000)
    service: str | None
    timeout: int


@router.post(
    "/api/projects/{project_name}/runs",
    status_code=202,
    dependencies=[Depends(get_api_key_sync)],
)
def run_post(
    dbsession: Annotated[DBSession, Depends(get_db_sync)],
    project: Annotated[Project, Depends(get_project_from_url_sync)],
    api_key: Annotated[ApiKey, Depends(get_api_key_sync)],
    req_body: RunReqBody,
    background_tasks: BackgroundTasks,
):
    deployment = get_live_deployment_sync(dbsession, project)
    if deployment is None:
        raise HTTPException(422, "Must deploy first")
    disco_file: DiscoFile = get_disco_file_from_str(deployment.disco_file)
    if req_body.service is None:
        if len(list(disco_file.services.keys())) == 0:
            raise HTTPException(422)
        if (
            "web" in disco_file.services
            and disco_file.services["web"].type != ServiceType.static
        ):
            service = "web"
        else:
            services = list(
                [
                    name
                    for name, service in disco_file.services.items()
                    if service.type != ServiceType.static
                ]
            )
            if len(services) == 0:
                raise HTTPException(422, "No service can run commands in project")
            service = services[0]
    else:
        if req_body.service not in disco_file.services:
            raise RequestValidationError(
                errors=(
                    ValidationError.from_exception_data(
                        "ValueError",
                        [
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "value_error",
                                    f'Service "{req_body.service}" not in Discofile: {list(disco_file.services.keys())}',
                                ),
                                loc=("body", "service"),
                                input=req_body.service,
                            )
                        ],
                    )
                ).errors()
            )
        if disco_file.services[req_body.service].type == ServiceType.static:
            raise RequestValidationError(
                errors=(
                    ValidationError.from_exception_data(
                        "ValueError",
                        [
                            InitErrorDetails(
                                type=PydanticCustomError(
                                    "value_error",
                                    f'Service "{req_body.service}" can\'t run commands',
                                ),
                                loc=("body", "service"),
                                input=req_body.service,
                            )
                        ],
                    )
                ).errors()
            )
        service = req_body.service
    command_run, func = create_command_run(
        dbsession=dbsession,
        project=project,
        deployment=deployment,
        service=service,
        command=req_body.command,
        timeout=req_body.timeout,
        by_api_key=api_key,
    )
    background_tasks.add_task(func)
    return {
        "run": {
            "number": command_run.number,
        },
    }


@router.get(
    "/api/projects/{project_name}/runs/{run_number}/output",
    dependencies=[Depends(get_api_key_wo_tx)],
)
async def run_output_get(
    project_name: str,
    run_number: int,
    last_event_id: Annotated[str | None, Header()] = None,
):
    with Session.begin() as dbsession:
        project = get_project_by_name_sync(dbsession, project_name)
        if project is None:
            raise HTTPException(status_code=404)
        run = get_command_run_by_number(dbsession, project, run_number)
        if run is None:
            raise HTTPException(status_code=404)
        after = None
        source = commandoutputs.run_source(run.id)
        if last_event_id is not None:
            output = await commandoutputs.get_by_id(source, last_event_id)
            if output is not None:
                after = output.created

    # TODO refactor, this is copy-pasted from deployment output
    async def get_run_output(source: str, after: datetime | None):
        while True:
            output = await commandoutputs.get_next(source, after=after)
            if output is not None:
                if output.text is None:
                    yield ServerSentEvent(
                        id=output.id,
                        event="end",
                        data="",
                    )
                    return
                after = output.created
                yield ServerSentEvent(
                    id=output.id,
                    event="output",
                    data=json.dumps(
                        {
                            "timestamp": output.created.isoformat(),
                            "text": output.text,
                        }
                    ),
                )
            if output is None:
                await asyncio.sleep(0.1)

    return EventSourceResponse(get_run_output(source, after))
