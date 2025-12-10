import asyncio
import fcntl
import json
import logging
import os
import pty
import struct
import termios
import time
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from disco.auth import validate_token
from disco.models.db import Session
from disco.utils import keyvalues
from disco.utils.deployments import get_live_deployment_sync
from disco.utils.discofile import DiscoFile, ServiceType, get_disco_file_from_str
from disco.utils.docker import (
    add_network_to_container,
    deployment_network_name,
    get_image_name_for_service,
    remove_container,
)
from disco.utils.encryption import decrypt
from disco.utils.projects import get_project_by_name_sync, volume_name_for_project

log = logging.getLogger(__name__)

router = APIRouter()

# Configuration for shell containers
SHELL_MAX_LIFETIME_SECONDS = 24 * 60 * 60  # 24 hours hard limit
SHELL_CPU_LIMIT = "0.5"  # 50% of one CPU
SHELL_MEMORY_LIMIT = "512m"  # 512 MB
SHELL_STOP_TIMEOUT = 5  # seconds
SHELL_HEARTBEAT_INTERVAL = 30  # seconds between ping messages


@router.websocket("/api/projects/{project_name}/shell")
async def project_shell(
    websocket: WebSocket,
    project_name: str,
):
    """
    Interactive shell session in a project's container.

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
    except (asyncio.TimeoutError, Exception):
        await websocket.close(code=4001, reason="Unauthorized")
        return

    token = msg.get("token")
    requested_service = msg.get("service")  # Optional
    requested_command = msg.get("command")  # Optional - for one-shot command execution

    if not token:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    api_key = await validate_token(token)
    if api_key is None:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    # ===== STEP 2: Get project and deployment info =====
    with Session.begin() as dbsession:
        project = get_project_by_name_sync(dbsession, project_name)
        if project is None:
            await websocket.close(code=4004, reason="Project not found")
            return

        deployment = get_live_deployment_sync(dbsession, project)
        if deployment is None:
            await websocket.close(code=4022, reason="No live deployment")
            return

        disco_file: DiscoFile = get_disco_file_from_str(deployment.disco_file)

        # Determine which service to use
        if requested_service:
            if requested_service not in disco_file.services:
                await websocket.close(code=4022, reason=f"Service '{requested_service}' not found")
                return
            if disco_file.services[requested_service].type == ServiceType.static:
                await websocket.close(code=4022, reason=f"Service '{requested_service}' is static")
                return
            service = requested_service
        else:
            service = resolve_service_for_shell(disco_file)
            if service is None:
                await websocket.close(code=4022, reason="No service can run shell")
                return

        # Gather container config
        registry_host = keyvalues.get_value_sync(dbsession, "REGISTRY_HOST")
        image = get_image_name_for_service(
            disco_file=disco_file,
            service_name=service,
            registry_host=registry_host,
            project_name=project.name,
            deployment_number=deployment.number,
        )

        env_variables = [
            (env_var.name, decrypt(env_var.value))
            for env_var in deployment.env_variables
        ]
        env_variables += [
            ("DISCO_PROJECT_NAME", project.name),
            ("DISCO_SERVICE_NAME", service),
            ("DISCO_HOST", keyvalues.get_value_str_sync(dbsession, "DISCO_HOST")),
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
        container_name = f"{project.name}-shell.{session_id[:8]}"
        created_at = int(time.time())
        expires_at = int(
            (datetime.utcnow() + timedelta(seconds=SHELL_MAX_LIFETIME_SECONDS)).timestamp()
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
        docker_args = ["docker", "run"]
        docker_args += ["--name", container_name]
        docker_args += ["--rm"]  # Auto-remove on exit
        docker_args += ["--restart=no"]  # Explicitly prevent restart

        # Labels for identification and cleanup
        docker_args += ["--label", f"disco.project.name={project_name}"]
        docker_args += ["--label", "disco.shell=true"]  # Easy filter
        docker_args += ["--label", f"disco.shell.created={created_at}"]
        docker_args += ["--label", f"disco.shell.expires_at={expires_at}"]

        # Resource limits - contain damage from orphans
        docker_args += ["--cpus", SHELL_CPU_LIMIT]
        docker_args += ["--memory", SHELL_MEMORY_LIMIT]

        # Short stop timeout so cleanup doesn't hang
        docker_args += ["--stop-timeout", str(SHELL_STOP_TIMEOUT)]

        docker_args += ["--interactive", "--tty"]
        # Disable logging to prevent terminal output (escape codes, etc.) from polluting logs
        docker_args += ["--log-driver", "none"]
        docker_args += ["--network", network]

        for var_name, var_value in env_variables:
            docker_args += ["--env", f"{var_name}={var_value}"]

        for volume_type, source, destination in volumes:
            docker_args += [
                "--mount",
                f"type={volume_type},source={source},destination={destination}",
            ]

        # Use /bin/sh which exists on all Linux systems (bash on Debian, ash on Alpine)
        if requested_command:
            # One-shot command mode: run command via sh -c
            docker_args += [image, "/bin/sh", "-c", requested_command]
        else:
            # Interactive shell mode
            docker_args += [image, "/bin/sh"]

        # Start container with PTY
        proc = await asyncio.create_subprocess_exec(
            *docker_args,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
        )
        os.close(slave_fd)  # Parent doesn't need slave
        slave_fd = None

        # Set master to non-blocking
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        # Also add to disco-main network for access to other services
        try:
            await add_network_to_container(container=container_name, network="disco-main")
        except Exception as e:
            log.warning("Failed to add disco-main network: %s", e)

        # Notify client we're connected
        await websocket.send_json({"type": "connected", "container": container_name})

        # ===== STEP 4: Bridge PTY <-> WebSocket =====
        await bridge_pty_websocket(websocket, master_fd, proc, is_command_mode=bool(requested_command))

        # Send exit message with exit code (useful for command mode)
        exit_code = proc.returncode if proc.returncode is not None else 0
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
        # Container should auto-remove due to --rm, but try cleanup just in case
        try:
            await remove_container(container_name)
        except Exception:
            pass  # Container likely already removed by --rm


async def bridge_pty_websocket(
    websocket: WebSocket,
    master_fd: int,
    proc: asyncio.subprocess.Process,
    is_command_mode: bool = False,
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
                message = await asyncio.wait_for(
                    websocket.receive(),
                    timeout=0.5
                )

                if message["type"] == "websocket.disconnect":
                    break

                if "bytes" in message:
                    os.write(master_fd, message["bytes"])
                elif "text" in message:
                    try:
                        ctrl = json.loads(message["text"])
                        if ctrl.get("type") == "resize":
                            log.info("Resize request: rows=%s, cols=%s", ctrl["rows"], ctrl["cols"])
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
                await asyncio.sleep(SHELL_HEARTBEAT_INTERVAL)
                if not exit_event.is_set():
                    await websocket.send_json({"type": "ping"})
            except Exception:
                break

    # Start process watcher
    watch_task = asyncio.create_task(watch_process())

    done, pending = await asyncio.wait(
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


def resolve_service_for_shell(disco_file: DiscoFile) -> str | None:
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
