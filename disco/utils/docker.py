import asyncio
import logging
import os
import signal
import subprocess
from datetime import datetime, timedelta, timezone
from multiprocessing import cpu_count
from typing import AsyncGenerator, Awaitable, Callable

from disco.utils.discofile import DiscoFile, Service
from disco.utils.filesystem import project_path

log = logging.getLogger(__name__)


async def build_image(
    image: str,
    project_name: str,
    env_variables: list[tuple[str, str]],
    stdout: Callable[[str], Awaitable[None]],
    stderr: Callable[[str], Awaitable[None]],
    context: str,
    dockerfile_path: str | None = None,
    dockerfile_str: str | None = None,
    timeout: int = 3600,
) -> None:
    assert (dockerfile_path is None) != (dockerfile_str is None)
    # include all env variables individually, and also include a .env with all variables
    env_var_args = []
    for key, _ in env_variables:
        env_var_args.append("--secret")
        env_var_args.append(f"id={key}")
    dot_env = "\n".join([f"{key}={value}" for key, value in env_variables]) + "\n"
    env_var_args.append("--secret")
    env_var_args.append("id=.env,env=DOT_ENV")
    env_variables += [("DOT_ENV", dot_env)]
    args = [
        "docker",
        "build",
        *env_var_args,
        "--cpu-period",
        "100000",  # default
        "--cpu-quota",
        # use half of the CPU time
        str(int(100000 * cpu_count() / 2)),
        "--tag",
        image,
        "--file",
        dockerfile_path if dockerfile_path is not None else "-",
        context,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            env=dict(env_variables),
            cwd=project_path(project_name),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if dockerfile_str is not None else None,
        )

        async def write_stdin() -> None:
            if dockerfile_str is None:
                return
            assert process.stdin is not None
            process.stdin.write(dockerfile_str.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.write_eof()

        async def read_stdout() -> None:
            assert process.stdout is not None
            async for line in process.stdout:
                await stdout(line.decode("utf-8"))

        async def read_stderr() -> None:
            assert process.stderr is not None
            async for line in process.stderr:
                await stderr(line.decode("utf-8"))

        tasks = [
            asyncio.create_task(write_stdin()),
            asyncio.create_task(read_stdout()),
            asyncio.create_task(read_stderr()),
        ]

        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(*tasks)
        except TimeoutError:
            process.terminate()
            raise Exception(f"Building image failed, timeout after {timeout} seconds")

        await process.wait()
        if process.returncode != 0:
            raise Exception(f"Docker returned status {process.returncode}")
    except asyncio.CancelledError:
        log.info("Killing build of image %s for project %s", image, project_name)
        os.kill(process.pid, signal.SIGKILL)
        await process.wait()
        log.warning("Killed build of image %s for project %s", image, project_name)
        raise


def start_service_sync(
    image: str,
    name: str,
    project_name: str,
    project_service_name: str,
    deployment_number: int,
    env_variables: list[tuple[str, str]],
    volumes: list[tuple[str, str, str]],
    published_ports: list[tuple[int, int, str]],
    networks: list[tuple[str, str]],
    replicas: int,
    command: str | None,
) -> None:
    more_args = []
    for var_name, var_value in env_variables:
        more_args.append("--env")
        more_args.append(f"{var_name}={var_value}")
    for volume_type, source, destination in volumes:
        assert volume_type == "volume"
        more_args.append("--mount")
        more_args.append(
            f"type={volume_type},source={source},destination={destination}"
        )
    if len(volumes) > 0:
        # volumes are on the main node
        more_args.append("--constraint")
        more_args.append("node.labels.disco-role==main")
    for host_port, container_port, protocol in published_ports:
        more_args.append("--publish")
        more_args.append(
            f"published={host_port},target={container_port},protocol={protocol}"
        )
    for network, alias in networks:
        more_args.append("--network")
        more_args.append(f"name={network},alias={alias}")
    args = [
        "docker",
        "service",
        "create",
        "--name",
        name,
        "--with-registry-auth",
        "--replicas",
        str(replicas),
        "--label",
        f"disco.project.name={project_name}",
        "--label",
        f"disco.service.name={project_service_name}",
        "--label",
        f"disco.deployment.number={deployment_number}",
        "--container-label",
        f"disco.project.name={project_name}",
        "--container-label",
        f"disco.service.name={project_service_name}",
        "--container-label",
        f"disco.deployment.number={deployment_number}",
        *more_args,
        image,
        *(command.split() if command is not None else []),
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    timeout_seconds = 900  # 15 minutes, safety net
    timeout = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    next_check = datetime.now(timezone.utc) + timedelta(seconds=3)
    for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)
        if datetime.now(timezone.utc) > next_check:
            states = get_service_nodes_desired_state(name)
            if len([state for state in states if state == "Shutdown"]) >= 3 * replicas:
                # 3 attempts to start the service failed
                process.terminate()
                raise Exception("Starting task failed, too many failed attempts")
            next_check += timedelta(seconds=3)
        if datetime.now(timezone.utc) > timeout:
            process.terminate()
            raise Exception(
                f"Starting task failed, timeout after {timeout_seconds} seconds"
            )

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


async def start_service(
    image: str,
    name: str,
    project_name: str,
    project_service_name: str,
    deployment_number: int,
    env_variables: list[tuple[str, str]],
    volumes: list[tuple[str, str, str]],
    published_ports: list[tuple[int, int, str]],
    networks: list[tuple[str, str]],
    replicas: int,
    command: str | None,
) -> None:
    more_args = []
    for var_name, var_value in env_variables:
        more_args.append("--env")
        more_args.append(f"{var_name}={var_value}")
    for volume_type, source, destination in volumes:
        assert volume_type == "volume"
        more_args.append("--mount")
        more_args.append(
            f"type={volume_type},source={source},destination={destination}"
        )
    if len(volumes) > 0:
        # volumes are on the main node
        more_args.append("--constraint")
        more_args.append("node.labels.disco-role==main")
    for host_port, container_port, protocol in published_ports:
        more_args.append("--publish")
        more_args.append(
            f"published={host_port},target={container_port},protocol={protocol}"
        )
    for network, alias in networks:
        more_args.append("--network")
        more_args.append(f"name={network},alias={alias}")
    args = [
        "docker",
        "service",
        "create",
        "--name",
        name,
        "--with-registry-auth",
        "--replicas",
        str(replicas),
        "--label",
        f"disco.project.name={project_name}",
        "--label",
        f"disco.service.name={project_service_name}",
        "--label",
        f"disco.deployment.number={deployment_number}",
        "--container-label",
        f"disco.project.name={project_name}",
        "--container-label",
        f"disco.service.name={project_service_name}",
        "--container-label",
        f"disco.deployment.number={deployment_number}",
        *more_args,
        image,
        *(command.split() if command is not None else []),
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    timeout_seconds = 900  # 15 minutes, safety net
    timeout = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    next_check = datetime.now(timezone.utc) + timedelta(seconds=3)
    async for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)
        if datetime.now(timezone.utc) > next_check:
            states = await get_service_nodes_desired_state_async(name)
            if len([state for state in states if state == "Shutdown"]) >= 3 * replicas:
                # 3 attempts to start the service failed
                process.terminate()
                raise Exception("Starting task failed, too many failed attempts")
            next_check += timedelta(seconds=3)
        if datetime.now(timezone.utc) > timeout:
            process.terminate()
            raise Exception(
                f"Starting task failed, timeout after {timeout_seconds} seconds"
            )

    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def get_service_nodes_desired_state(service_name: str) -> list[str]:
    args = [
        "docker",
        "service",
        "ps",
        service_name,
        "--format",
        "{{ .DesiredState }}",
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    states = [line.decode("utf-8")[:-1] for line in process.stdout.readlines()]
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    return states


async def get_service_nodes_desired_state_async(service_name: str) -> list[str]:
    args = [
        "docker",
        "service",
        "ps",
        service_name,
        "--format",
        "{{ .DesiredState }}",
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    states = [line.decode("utf-8")[:-1] async for line in process.stdout]
    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    return states


async def push_image(image: str) -> None:
    log.info("Pushing image %s", image)
    timeout = 3600
    args = [
        "docker",
        "push",
        image,
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def read_stdout() -> None:
            assert process.stdout is not None
            async for line in process.stdout:
                log.info("Stdout: %s", line.decode("utf-8").replace("\n", ""))

        async def read_stderr() -> None:
            assert process.stderr is not None
            async for line in process.stderr:
                log.info("Stderr: %s", line.decode("utf-8").replace("\n", ""))

        tasks = [
            asyncio.create_task(read_stdout()),
            asyncio.create_task(read_stderr()),
        ]

        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(*tasks)
        except TimeoutError:
            process.terminate()
            raise Exception(f"Running command failed, timeout after {timeout} seconds")

        await process.wait()
        if process.returncode != 0:
            raise Exception(f"Docker returned status {process.returncode}")
    except asyncio.CancelledError:
        log.info("Killing pushing image %s", image)
        os.kill(process.pid, signal.SIGKILL)
        await process.wait()
        log.info("Killed pushing image %s", image)
        raise


def stop_service_sync(name: str) -> None:
    log.info("Stopping service %s", name)
    args = [
        "docker",
        "service",
        "rm",
        name,
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


async def stop_service(name: str) -> None:
    log.info("Stopping service %s", name)
    args = [
        "docker",
        "service",
        "rm",
        name,
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    async for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


async def get_log_for_service(service_name: str) -> str:
    args = [
        "docker",
        "service",
        "logs",
        "--raw",
        service_name,
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    # Sometimes "docker service logs" hangs.
    # It seems like a bug on their side.
    # That's why we have those timeouts.
    # It makes this function suitable to report
    # when a service fails to start, but probably
    # not for other purposes.
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 5)
    except asyncio.TimeoutError:
        process.kill()
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 5)
        except asyncio.TimeoutError:
            return ""

    return stdout.decode("utf-8")


async def network_exists(network_name: str) -> bool:
    args = [
        "docker",
        "network",
        "inspect",
        network_name,
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await process.wait()
    return process.returncode == 0


def service_exists_sync(service_name: str) -> bool:
    args = [
        "docker",
        "service",
        "inspect",
        service_name,
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process.wait()
    return process.returncode == 0


async def service_exists(service_name: str) -> bool:
    args = [
        "docker",
        "service",
        "inspect",
        service_name,
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await process.wait()
    return process.returncode == 0


def list_services_for_project_sync(project_name: str) -> list[str]:
    args = [
        "docker",
        "service",
        "ls",
        "--filter",
        f"label=disco.project.name={project_name}",
        "--format",
        "{{ .Name }}",
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    services = [line.decode("utf-8")[:-1] for line in process.stdout.readlines()]
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    return services


async def list_services_for_project(project_name: str) -> list[str]:
    args = [
        "docker",
        "service",
        "ls",
        "--filter",
        f"label=disco.project.name={project_name}",
        "--format",
        "{{ .Name }}",
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    services = []
    async for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        services.append(line_text)
    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    return services


def list_containers_for_project(project_name: str) -> list[str]:
    args = [
        "docker",
        "container",
        "ls",
        "-a",
        "--filter",
        f"label=disco.project.name={project_name}",
        "--format",
        "{{ .Names }}",
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    containers = [
        line.decode("utf-8")[:-1].split(",")[0] for line in process.stdout.readlines()
    ]
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    return containers


async def list_services_for_deployment(
    project_name: str, deployment_number: int
) -> list[str]:
    args = [
        "docker",
        "service",
        "ls",
        "--filter",
        f"label=disco.project.name={project_name}",
        "--filter",
        f"label=disco.deployment.number={deployment_number}",
        "--format",
        "{{ .Name }}",
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    services = []
    async for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        services.append(line_text)
    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    return services


async def list_networks_for_project(project_name: str) -> list[str]:
    args = [
        "docker",
        "network",
        "ls",
        "--filter",
        f"label=disco.project.name={project_name}",
        "--format",
        "{{ .Name }}",
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    networks = []
    async for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        networks.append(line_text)
    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    return networks


async def list_networks_for_deployment(
    project_name: str, deployment_number: int
) -> list[str]:
    args = [
        "docker",
        "network",
        "ls",
        "--filter",
        f"label=disco.project.name={project_name}",
        "--filter",
        f"label=disco.deployment.number={deployment_number}",
        "--format",
        "{{ .Name }}",
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    networks = []
    async for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        networks.append(line_text)
    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    return networks


def internal_image_name(
    registry_host: str | None,
    project_name: str,
    deployment_number: int,
    image_name: str,
) -> str:
    base_name = f"disco/project-{project_name}-{image_name}:{deployment_number}"
    if registry_host is None:
        return base_name
    return f"{registry_host}/{base_name}"


def service_name(project_name: str, service: str, deployment_number: int) -> str:
    return f"{project_name}-{deployment_number}-{service}"


async def set_syslog_service(disco_host: str, syslog_urls: list[str]) -> None:
    if len(syslog_urls) == 0:
        if await service_exists("disco-syslog"):
            await stop_service("disco-syslog")
        else:
            log.info("Syslog service already stopped")
    else:
        if await service_exists("disco-syslog"):
            await _update_syslog_service(disco_host, syslog_urls)
        else:
            await _start_syslog_service(disco_host, syslog_urls)


async def _start_syslog_service(disco_host: str, syslog_urls: list[str]) -> None:
    node_count = await get_node_count()
    log.info("Starting syslog service")
    args = [
        "docker",
        "service",
        "create",
        "--name",
        "disco-syslog",
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
        "--env",
        f"SYSLOG_HOSTNAME={disco_host}",
        "--mode",
        "global",
        "gliderlabs/logspout",
        ",".join(syslog_urls),
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    timeout_seconds = 900  # 15 minutes, safety net
    timeout = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    next_check = datetime.now(timezone.utc) + timedelta(seconds=3)
    async for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)
        if datetime.now(timezone.utc) > next_check:
            states = await get_service_nodes_desired_state_async("disco-syslog")
            if (
                len([state for state in states if state == "Shutdown"])
                >= 3 * node_count
            ):
                # 3 attempts to start the service failed
                process.terminate()
                raise Exception("Starting task failed, too many failed attempts")
            next_check += timedelta(seconds=3)
        if datetime.now(timezone.utc) > timeout:
            process.terminate()
            raise Exception(
                f"Starting task failed, timeout after {timeout_seconds} seconds"
            )

    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")

    log.info("Syslog service started")


async def _update_syslog_service(disco_host: str, syslog_urls: list[str]) -> None:
    node_count = await get_node_count()
    log.info("Updating syslog service")
    args = [
        "docker",
        "service",
        "update",
        "disco-syslog",
        "--env-add",
        f"SYSLOG_HOSTNAME={disco_host}",
        "--args",
        ",".join(syslog_urls),
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    timeout_seconds = 900  # 15 minutes, safety net
    timeout = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    next_check = datetime.now(timezone.utc) + timedelta(seconds=3)
    async for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)
        if datetime.now(timezone.utc) > next_check:
            states = await get_service_nodes_desired_state_async("disco-syslog")
            if (
                len([state for state in states if state == "Shutdown"])
                >= 3 * node_count
            ):
                # 3 attempts to start the service failed
                process.terminate()
                raise Exception("Starting task failed, too many failed attempts")
            next_check += timedelta(seconds=3)
        if datetime.now(timezone.utc) > timeout:
            process.terminate()
            raise Exception(
                f"Starting task failed, timeout after {timeout_seconds} seconds"
            )

    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")

    log.info("Syslog service updated")


async def get_node_count() -> int:
    log.info("Getting Docker Swarm node count")
    args = [
        "docker",
        "info",
        "--format",
        "{{ .Swarm.Nodes }}",
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    async for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)
        node_count = int(line_text)
        break

    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    return node_count


def create_network_sync(
    name: str, project_name: str | None = None, deployment_number: int | None = None
) -> None:
    log.info("Creating network %s", name)
    more_args = []
    if project_name is not None:
        more_args += [
            "--label",
            f"disco.project.name={project_name}",
        ]
    if deployment_number is not None:
        more_args += [
            "--label",
            f"disco.deployment.number={deployment_number}",
        ]
    args = [
        "docker",
        "network",
        "create",
        "--driver",
        "overlay",
        "--attachable",
        "--opt",
        "encrypted",
        *more_args,
        name,
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


async def create_network(
    name: str, project_name: str | None = None, deployment_number: int | None = None
) -> None:
    log.info("Creating network %s", name)
    more_args = []
    if project_name is not None:
        more_args += [
            "--label",
            f"disco.project.name={project_name}",
        ]
    if deployment_number is not None:
        more_args += [
            "--label",
            f"disco.deployment.number={deployment_number}",
        ]
    args = [
        "docker",
        "network",
        "create",
        "--driver",
        "overlay",
        "--attachable",
        "--opt",
        "encrypted",
        *more_args,
        name,
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if stdout is not None:
        for line in stdout.decode("utf-8").split("\n"):
            if len(line) > 0:
                log.info("Stdout: %s", line)
    if stderr is not None:
        for line in stderr.decode("utf-8").split("\n"):
            if len(line) > 0:
                log.info("Stderr: %s", line)
    await process.wait()

    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def pull(image: str) -> None:
    log.info("Pulling Docker image %s", image)
    args = [
        "docker",
        "pull",
        image,
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def remove_network_sync(name: str) -> None:
    # XXX Do not remove networks, as Docker Swarm fails to free IP addresses
    # https://github.com/moby/moby/issues/37338
    log.info("Removing network %s", name)
    args = [
        "docker",
        "network",
        "rm",
        name,
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def add_network_to_container(
    container: str, network: str, alias: str | None = None
) -> None:
    log.info("Adding network to container: %s to %s", network, container)
    more_args = []
    if alias is not None:
        more_args += ["--alias", alias]
    args = [
        "docker",
        "network",
        "connect",
        *more_args,
        network,
        container,
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


async def add_network_to_container_async(container: str, network: str) -> None:
    log.info("Adding network to container: %s to %s", network, container)
    args = [
        "docker",
        "network",
        "connect",
        network,
        container,
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    async for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def remove_network_from_container(container: str, network: str) -> None:
    log.info("Removing network from container: %s from %s", network, container)
    args = [
        "docker",
        "network",
        "disconnect",
        network,
        container,
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def run_sync(
    image: str,
    project_name: str,
    name: str,
    env_variables: list[tuple[str, str]],
    volumes: list[tuple[str, str, str]],
    networks: list[str],
    command: str | None,
    log_output: Callable[[str], None],
    workdir: str | None = None,
    timeout: int = 600,
) -> None:
    try:
        more_args = []
        for var_name, var_value in env_variables:
            more_args.append("--env")
            more_args.append(f"{var_name}={var_value}")
        for volume_type, source, destination in volumes:
            assert volume_type in ["bind", "volume"]
            more_args.append("--mount")
            more_args.append(
                f"type={volume_type},source={source},destination={destination}"
            )
        if workdir is not None:
            more_args.append("--workdir")
            more_args.append(workdir)
        args = [
            "docker",
            "container",
            "create",
            "--name",
            name,
            "--label",
            f"disco.project.name={project_name}",
            "--label",
            f"disco.service.name={name}",
            *more_args,
            image,
            *(command.split() if command is not None else []),
        ]
        process = subprocess.Popen(
            args=args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert process.stdout is not None
        timeout_dt = datetime.now(timezone.utc) + timedelta(seconds=timeout)
        for line in process.stdout:
            line_text = line.decode("utf-8")
            if line_text.endswith("\n"):
                line_text = line_text[:-1]
            log.info("Output: %s", line_text)
            if datetime.now(timezone.utc) > timeout_dt:
                process.terminate()
                raise Exception(
                    f"Running command failed, timeout after {timeout} seconds"
                )

        process.wait()
        if process.returncode != 0:
            raise Exception(f"Docker returned status {process.returncode}")
        for network in networks:
            add_network_to_container(container=name, network=network)
        args = [
            "docker",
            "container",
            "start",
            "--attach",
            name,
        ]
        process = subprocess.Popen(
            args=args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert process.stdout is not None
        timeout_dt = datetime.now(timezone.utc) + timedelta(seconds=timeout)
        for line in process.stdout:
            log_output(line.decode("utf-8"))
            if datetime.now(timezone.utc) > timeout_dt:
                process.terminate()
                raise Exception(
                    f"Running command failed, timeout after {timeout} seconds"
                )

        process.wait()
        if process.returncode != 0:
            raise Exception(f"Docker returned status {process.returncode}")
    finally:
        remove_container_sync(name)


async def run(
    image: str,
    project_name: str,
    name: str,
    env_variables: list[tuple[str, str]],
    volumes: list[tuple[str, str, str]],
    networks: list[str],
    command: str | None,
    stdout: Callable[[str], Awaitable[None]],
    stderr: Callable[[str], Awaitable[None]],
    stdin: AsyncGenerator[bytes, None] | None = None,
    workdir: str | None = None,
    timeout: int = 600,
) -> None:
    try:
        more_args = []
        for var_name, var_value in env_variables:
            more_args.append("--env")
            more_args.append(f"{var_name}={var_value}")
        for volume_type, source, destination in volumes:
            assert volume_type in ["bind", "volume"]
            more_args.append("--mount")
            more_args.append(
                f"type={volume_type},source={source},destination={destination}"
            )
        if workdir is not None:
            more_args.append("--workdir")
            more_args.append(workdir)
        if stdin is not None:
            more_args.append("--interactive")
        args = [
            "docker",
            "container",
            "create",
            "--name",
            name,
            "--label",
            f"disco.project.name={project_name}",
            "--label",
            f"disco.service.name={name}",
            *more_args,
            image,
            *(command.split() if command is not None else []),
        ]
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert process.stdout is not None

        timeout_dt = datetime.now(timezone.utc) + timedelta(seconds=timeout)
        async for line in process.stdout:
            line_text = line.decode("utf-8")
            if line_text.endswith("\n"):
                line_text = line_text[:-1]
            log.info("Output: %s", line_text)
            if datetime.now(timezone.utc) > timeout_dt:
                process.terminate()
                raise Exception(
                    f"Running command failed, timeout after {timeout} seconds"
                )

        await process.wait()
        if process.returncode != 0:
            raise Exception(f"Docker returned status {process.returncode}")
        for network in networks:
            await add_network_to_container_async(container=name, network=network)
        more_args = []
        if stdin is not None:
            more_args.append("--interactive")
        args = [
            "docker",
            "container",
            "start",
            "--attach",
            *more_args,
            name,
        ]
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        )

        async def write_stdin() -> None:
            if stdin is None:
                return
            assert process.stdin is not None
            async for chunk in stdin:
                process.stdin.write(chunk)
                await process.stdin.drain()
            process.stdin.write_eof()

        async def read_stdout() -> None:
            assert process.stdout is not None
            async for line in process.stdout:
                await stdout(line.decode("utf-8"))

        async def read_stderr() -> None:
            assert process.stderr is not None
            async for line in process.stderr:
                await stderr(line.decode("utf-8"))

        tasks = [
            asyncio.create_task(write_stdin()),
            asyncio.create_task(read_stdout()),
            asyncio.create_task(read_stderr()),
        ]

        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(*tasks)
        except TimeoutError:
            process.terminate()
            raise Exception(f"Running command failed, timeout after {timeout} seconds")

        await process.wait()
        if process.returncode != 0:
            raise Exception(f"Docker returned status {process.returncode}")
    finally:
        await remove_container(name)


def remove_container_sync(name: str) -> None:
    log.info("Removing container %s", name)
    args = [
        "docker",
        "container",
        "rm",
        "--force",
        name,
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


async def remove_container(name: str) -> None:
    log.info("Removing container %s", name)
    args = [
        "docker",
        "container",
        "rm",
        "--force",
        name,
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    async for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def deployment_network_name(project_name: str, deployment_number: int) -> str:
    return f"disco-project-{project_name}-{deployment_number}"


def get_image_name_for_service(
    disco_file: DiscoFile,
    service_name: str,
    registry_host: str | None,
    project_name: str,
    deployment_number: int,
) -> str:
    if service_name not in disco_file.services:
        raise Exception(
            f"Service {service_name} not in Discofile: {list(disco_file.services.keys())}"
        )
    service = disco_file.services[service_name]
    if service.build is not None:
        # has a build command, is named after service name
        return internal_image_name(
            registry_host=registry_host,
            project_name=project_name,
            deployment_number=deployment_number,
            image_name=service_name,
        )
    if service.image in disco_file.images:
        # image defined in Discofile
        return internal_image_name(
            registry_host=registry_host,
            project_name=project_name,
            deployment_number=deployment_number,
            image_name=service.image,
        )
    else:
        # image hosted in a Docker registry
        return service.image


def login(disco_host_home: str, host: str, username: str, password: str) -> None:
    import disco

    log.info("Docker login to %s", host)
    args = [
        "docker",
        "run",
        "--rm",
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
        "--mount",
        f"type=bind,source={disco_host_home},target=/root",
        f"letsdiscodev/daemon:{disco.__version__}",
        "docker",
        "login",
        "--username",
        username,
        "--password",
        password,
        f"https://{host}",
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def get_swarm_join_token() -> str:
    log.info("Getting Docker Swarm join token")
    args = [
        "docker",
        "swarm",
        "join-token",
        "--quiet",
        "worker",
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    output = ""
    for line in process.stdout:
        output += line.decode("utf-8")

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    token = output.split("\n")[0]
    return token


def scale(services: dict[str, int]) -> None:
    log.info("Scaling services %s", " ".join([f"{s}={n}" for s, n in services.items()]))
    args = [
        "docker",
        "service",
        "scale",
        "--detach",
        *[f"{service_name}={scale}" for service_name, scale in services.items()],
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    for line in process.stdout:
        line_text = line.decode("utf-8")
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)


async def get_image_workdir(image: str) -> str:
    args = [
        "docker",
        "image",
        "inspect",
        image,
        "--format={{.Config.WorkingDir}}",
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    out, _ = await process.communicate()
    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    workdir = out.decode("utf-8").replace("\n", "")
    return workdir


async def copy_files_from_image(image: str, src: str, dst: str) -> None:
    args = [
        "docker",
        "container",
        "create",
        image,
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    out, _ = await process.communicate()
    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    container_name = out.decode("utf-8").replace("\n", "")
    # transform /code/dist to /code/dist/.
    if not src.endswith("."):
        if not src.endswith("/"):
            src += "/"
        src += "."
    args = [
        "docker",
        "cp",
        f"{container_name}:{src}",
        dst,
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    _, _ = await process.communicate()
    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    await remove_container(container_name)


async def start_container(
    image: str,
    name: str,
    env_variables: list[tuple[str, str]],
    volumes: list[tuple[str, str, str]],
    published_ports: list[tuple[int, int, str]],
    networks: list[str],
    command: str | None,
    workdir: str | None = None,
) -> None:
    more_args = []
    for var_name, var_value in env_variables:
        more_args.append("--env")
        more_args.append(f"{var_name}={var_value}")
    for volume_type, source, destination in volumes:
        assert volume_type in ["bind", "volume"]
        more_args.append("--mount")
        more_args.append(
            f"type={volume_type},source={source},destination={destination}"
        )
    if workdir is not None:
        more_args.append("--workdir")
        more_args.append(workdir)
    for host_port, container_port, protocol in published_ports:
        more_args.append("--publish")
        more_args.append(
            f"published={host_port},target={container_port},protocol={protocol}"
        )
    args = [
        "docker",
        "container",
        "create",
        "--name",
        name,
        *more_args,
        image,
        *(command.split() if command is not None else []),
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if len(stdout) > 0:
        log.info("Stdout: %s", stdout)
    if len(stderr) > 0:
        log.info("Stderr: %s", stderr)
    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    for network in networks:
        await add_network_to_container_async(container=name, network=network)
    args = [
        "docker",
        "container",
        "start",
        name,
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()
    if len(stdout) > 0:
        log.info("Stdout: %s", stdout)
    if len(stderr) > 0:
        log.info("Stderr: %s", stderr)
    await process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


EASY_MODE_DOCKERFILE = """
FROM {image}
WORKDIR /project
COPY . /project
RUN --mount=type=secret,id=.env env $(cat /run/secrets/.env | xargs) {command}
"""


def easy_mode_dockerfile(service: Service) -> str:
    return EASY_MODE_DOCKERFILE.format(image=service.image, command=service.build)
