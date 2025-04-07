import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from multiprocessing import cpu_count
from typing import AsyncGenerator, Awaitable, Callable, Literal

import disco
from disco.errors import ProcessStatusError
from disco.utils.discofile import DiscoFile
from disco.utils.discofile import Service as DiscoService
from disco.utils.filesystem import project_path
from disco.utils.subprocess import check_call, decode_text

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
    log.info("Building Docker image %s", image)
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
                await stdout(decode_text(line))

        async def read_stderr() -> None:
            assert process.stderr is not None
            async for line in process.stderr:
                await stderr(decode_text(line))

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
    log.info("Starting Docker service %s", name)
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
        line_text = decode_text(line)
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)
        if datetime.now(timezone.utc) > next_check:
            states = get_service_nodes_desired_state_sync(name)
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


async def start_project_service(
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
    log.info("Starting Docker project service %s", name)
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
        line_text = decode_text(line)
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)
        if datetime.now(timezone.utc) > next_check:
            states = await get_service_nodes_desired_state(name)
            if (
                replicas > 0
                and len([state for state in states if state == "Shutdown"])
                >= 3 * replicas
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


def get_service_nodes_desired_state_sync(service_name: str) -> list[str]:
    log.info("Getting Docker service nodes desired states: %s", service_name)
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
    states = [decode_text(line)[:-1] for line in process.stdout.readlines()]
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    return states


async def get_service_nodes_desired_state(service_name: str) -> list[str]:
    log.info("Getting Docker service nodes desired states: %s", service_name)
    args = [
        "docker",
        "service",
        "ps",
        service_name,
        "--format",
        "{{ .DesiredState }}",
    ]
    stdout, _, _ = await check_call(args)
    return stdout


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
                log.info("Stdout: %s", decode_text(line).replace("\n", ""))

        async def read_stderr() -> None:
            assert process.stderr is not None
            async for line in process.stderr:
                log.info("Stderr: %s", decode_text(line).replace("\n", ""))

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


async def rm_service(name: str) -> None:
    log.info("Stopping service %s", name)
    args = [
        "docker",
        "service",
        "rm",
        name,
    ]
    await check_call(args)


async def get_log_for_service(service_name: str) -> str:
    log.info("Getting logs for Docker service %s", service_name)
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

    return decode_text(stdout)


async def network_exists(network_name: str) -> bool:
    log.info("Checking if Docker network exists: %s", network_name)
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
    log.info("Checking if Docker service exists: %s", service_name)
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
    log.info("Checking if Docker service exists: %s", service_name)
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


async def list_services_for_project(project_name: str) -> list[str]:
    log.info("Listing Docker services for projecct %s", project_name)
    args = [
        "docker",
        "service",
        "ls",
        "--filter",
        f"label=disco.project.name={project_name}",
        "--format",
        "{{ .Name }}",
    ]
    stdout, _, _ = await check_call(args)
    return stdout


async def list_containers_for_project(project_name: str) -> list[str]:
    log.info("Listing Docker containers for projecct %s", project_name)
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
    stdout, _, _ = await check_call(args)
    return [line.split(",")[0] for line in stdout]


@dataclass
class Service:
    name: str
    replicas: int


async def list_services_for_deployment(
    project_name: str, deployment_number: int
) -> list[Service]:
    log.info(
        "Listing Docker services for deployment %s %d", project_name, deployment_number
    )
    args = [
        "docker",
        "service",
        "ls",
        "--filter",
        f"label=disco.project.name={project_name}",
        "--filter",
        f"label=disco.deployment.number={deployment_number}",
        "--format",
        '{"name":"{{.Name}}", "replicas":"{{.Replicas}}"}',
    ]
    stdout, _, _ = await check_call(args)
    services = []
    for line in stdout:
        try:
            service_data = json.loads(line)
        except json.decoder.JSONDecodeError:
            log.error("Could not JSON info for service: '%s'", line)
            continue
        service = Service(
            name=re.sub(
                f"^{re.escape(project_name)}-{deployment_number}-",
                "",
                service_data["name"],
            ),
            replicas=int(service_data["replicas"].split("/")[1]),
        )
        services.append(service)
    return services


async def list_networks_for_project(project_name: str) -> list[str]:
    log.info("Listing networks for project %s", project_name)
    args = [
        "docker",
        "network",
        "ls",
        "--filter",
        f"label=disco.project.name={project_name}",
        "--format",
        "{{ .Name }}",
    ]
    stdout, _, _ = await check_call(args)
    return stdout


async def list_networks_for_deployment(
    project_name: str, deployment_number: int
) -> list[str]:
    log.info("Listing networks for deployment %s %d", project_name, deployment_number)
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
    stdout, _, _ = await check_call(args)
    return stdout


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


@dataclass
class SyslogService:
    name: str
    type: str
    url: str


async def list_syslog_services() -> list[SyslogService]:
    log.info("Listing Docker syslog services")
    args = [
        "docker",
        "service",
        "ls",
        "--filter",
        "label=disco.syslog",
        "-q",
    ]
    service_ids, _, _ = await check_call(args)
    if len(service_ids) == 0:
        return []
    args = [
        "docker",
        "service",
        "inspect",
    ] + service_ids
    stdout, _, _ = await check_call(args)
    services_json = "\n".join(stdout)
    services_data = json.loads(services_json)
    services = []
    for service_data in services_data:
        service = SyslogService(
            name=service_data["Spec"]["Name"],
            type=service_data["Spec"]["Labels"]["disco.syslog.type"],
            url=service_data["Spec"]["Labels"]["disco.syslog.url"],
        )
        services.append(service)
    return services


def _logspout_url(url: str, type: Literal["CORE", "GLOBAL"]) -> str:
    if type == "CORE":
        return f"{url}?filter.labels=disco.log.core:true"
    assert type == "GLOBAL"
    return url


async def start_syslog_service(
    disco_host: str, url: str, type: Literal["CORE", "GLOBAL"]
) -> None:
    log.info("Starting Syslog service %s %s", url, type)
    syslog_url = _logspout_url(url=url, type=type)
    args = [
        "docker",
        "service",
        "create",
        "--name",
        f"disco-syslog-{uuid.uuid4().hex}",
        "--detach",
        "--label",
        "disco.syslog",
        "--label",
        f"disco.syslog.url={url}",
        "--label",
        f"disco.syslog.type={type}",
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
        "--env",
        f"SYSLOG_HOSTNAME={disco_host}",
        "--env",
        "EXCLUDE_LABELS=disco.log.exclude",
        "--mode",
        "global",
        "gliderlabs/logspout:latest",
        syslog_url,
    ]
    await check_call(args)


async def get_node_count() -> int:
    log.info("Getting Docker Swarm node count")
    args = [
        "docker",
        "info",
        "--format",
        "{{ .Swarm.Nodes }}",
    ]
    stdout, _, _ = await check_call(args)
    return int(stdout[0])


async def get_node_list() -> list[str]:
    log.info("Getting Docker Swarm node ID list")
    args = [
        "docker",
        "node",
        "ls",
        "--format",
        "{{ .ID }}",
    ]
    stdout, _, _ = await check_call(args)
    return stdout


@dataclass
class NodeDetails:
    id: str
    created: str
    labels: dict[str, str]
    role: str
    availability: str
    architecture: str
    state: str
    address: str


async def get_node_details(node_ids: list[str]) -> list[NodeDetails]:
    log.info("Getting Docker Swarm nodes details")
    args = [
        "docker",
        "node",
        "inspect",
    ] + node_ids
    stdout, _, _ = await check_call(args)
    nodes = json.loads("\n".join(stdout))
    return [
        NodeDetails(
            id=node["ID"],
            created=node["CreatedAt"],
            labels=node["Spec"]["Labels"],
            role=node["Spec"]["Role"],
            availability=node["Spec"]["Availability"],
            architecture=node["Description"]["Platform"],
            state=node["Status"]["State"],
            address=node["Status"]["Addr"],
        )
        for node in nodes
    ]


async def set_node_label(node_id: str, key: str, value: str) -> None:
    log.info("Setting Docker node label %s=%s for node %s", key, value, node_id)
    args = [
        "docker",
        "node",
        "update",
        "--label-add",
        f"{key}={value}",
        node_id,
    ]
    await check_call(args)


async def leave_swarm(node_id: str) -> str:
    log.info("Running command for node to leave the Docker Swarm %s", node_id)
    service_name = f"leave-swarm-{node_id}"
    args = [
        "docker",
        "service",
        "create",
        "--name",
        service_name,
        "--mode",
        "replicated-job",
        "--constraint",
        f"node.id=={node_id}",
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
        f"letsdiscodev/daemon:{disco.__version__}",
        "docker",
        "run",
        "--rm",
        "--detach",
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
        f"letsdiscodev/daemon:{disco.__version__}",
        "disco_leave_swarm",
    ]
    await check_call(args)
    return service_name


async def remove_node(node_id: str, force: bool = False) -> None:
    log.info("Removing Docker node %s", node_id)
    args = [
        "docker",
        "node",
        "rm",
        node_id,
    ]
    if force:
        args.append("--force")
    await check_call(args)


async def drain_node(node_id: str) -> None:
    log.info("Removing Docker node %s", node_id)
    args = [
        "docker",
        "node",
        "update",
        "--availability",
        "drain",
        node_id,
    ]
    await check_call(args)


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
    await check_call(args)


async def pull(image: str) -> None:
    log.info("Pulling Docker image %s", image)
    args = [
        "docker",
        "pull",
        image,
    ]
    await check_call(args)


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
        line_text = decode_text(line)
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def add_network_to_container_sync(
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
        line_text = decode_text(line)
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        log.info("Output: %s", line_text)

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


async def add_network_to_container(container: str, network: str) -> None:
    log.info("Adding network to container: %s to %s", network, container)
    args = [
        "docker",
        "network",
        "connect",
        network,
        container,
    ]
    await check_call(args)


async def remove_network_from_container(container: str, network: str) -> None:
    log.info("Removing network from container: %s from %s", network, container)
    args = [
        "docker",
        "network",
        "disconnect",
        network,
        container,
    ]
    await check_call(args)


class CommandRunProcessStatusError(ProcessStatusError):
    pass


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
    log.info("Docker run %s (%s)", name, image)
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

        async def read_create_container_stdout() -> None:
            assert process.stdout is not None
            async for line in process.stdout:
                line_text = decode_text(line)
                if line_text.endswith("\n"):
                    line_text = line_text[:-1]
                log.info("Output: %s", line_text)

        try:
            async with asyncio.timeout(timeout):
                await asyncio.wait_for(read_create_container_stdout(), timeout)
        except TimeoutError:
            process.terminate()
            raise

        await process.wait()
        if process.returncode != 0:
            raise ProcessStatusError(status=process.returncode)
        for network in networks:
            await add_network_to_container(container=name, network=network)
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
                await stdout(decode_text(line))

        async def read_stderr() -> None:
            assert process.stderr is not None
            async for line in process.stderr:
                await stderr(decode_text(line))

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
            raise

        await process.wait()
        if process.returncode != 0:
            raise CommandRunProcessStatusError(status=process.returncode)
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
        line_text = decode_text(line)
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
    await check_call(args)


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


async def login(disco_host_home: str, host: str, username: str, password: str) -> None:
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
        "--interactive",
        f"letsdiscodev/daemon:{disco.__version__}",
        "docker",
        "login",
        "--username",
        username,
        "--password-stdin",
        f"https://{host}",
    ]
    await check_call(args, stdin=password)


async def logout(disco_host_home: str, host: str) -> None:
    import disco

    log.info("Docker logout from %s", host)
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
        "logout",
        f"https://{host}",
    ]
    await check_call(args)


async def get_swarm_join_token() -> str:
    log.info("Getting Docker Swarm join token")
    args = [
        "docker",
        "swarm",
        "join-token",
        "--quiet",
        "worker",
    ]
    stdout, _, _ = await check_call(args)
    return stdout[0]


async def scale(services: dict[str, int]) -> None:
    log.info("Scaling services %s", " ".join([f"{s}={n}" for s, n in services.items()]))
    args = [
        "docker",
        "service",
        "scale",
        "--detach",
        *[f"{service_name}={scale}" for service_name, scale in services.items()],
    ]
    await check_call(args)


async def get_image_workdir(image: str) -> str:
    log.info("Getting image Workdir: %s", image)
    args = [
        "docker",
        "image",
        "inspect",
        image,
        "--format={{.Config.WorkingDir}}",
    ]
    stdout, _, _ = await check_call(args)
    return stdout[0]


async def copy_files_from_image(image: str, src: str, dst: str) -> None:
    log.info("Copying files from image %s (%s) to %s", image, src, dst)
    args = [
        "docker",
        "container",
        "create",
        image,
    ]
    stdout, _, _ = await check_call(args)
    container_name = stdout[0]
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
    await check_call(args)
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
    log.info("Starting Docker container %s (%s)", name, image)
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
    await check_call(args)
    for network in networks:
        await add_network_to_container(container=name, network=network)
    args = [
        "docker",
        "container",
        "start",
        name,
    ]
    await check_call(args)


async def ls_images_swarm() -> list[tuple[str, str]]:
    log.info("Listing Docker images in all nodes of Docker Swarm")
    LS_SERVICE_NAME = "disco-ls-images"
    images = set()
    try:
        args = [
            "docker",
            "service",
            "create",
            "--name",
            LS_SERVICE_NAME,
            "--mode",
            "global-job",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            f"letsdiscodev/daemon:{disco.__version__}",
            "docker",
            "image",
            "ls",
            "--format",
            '{"repository": "{{.Repository}}", "tag": "{{.Tag}}"}',
        ]
        await check_call(args)
        output = await get_log_for_service(LS_SERVICE_NAME)
        for line in output.split("\n"):
            if len(line.strip()) == 0:
                continue
            image = json.loads(line)
            images.add((image["repository"], image["tag"]))
    finally:
        await rm_service(LS_SERVICE_NAME)
    return list(images)


async def rm_image_swarm(image: str) -> None:
    log.info("Removing image from all nodes in Docker Swarm: %s", image)
    RM_SERVICE_NAME = "disco-rm-images"
    try:
        args = [
            "docker",
            "service",
            "create",
            "--name",
            RM_SERVICE_NAME,
            "--mode",
            "global-job",
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
            f"letsdiscodev/daemon:{disco.__version__}",
            "sh",
            "-c",
            f"docker image rm {image} 2>/dev/null || true",
        ]
        await check_call(args)
    finally:
        await rm_service(RM_SERVICE_NAME)


async def get_docker_version() -> str:
    log.info("Getting Docker version")
    args = [
        "docker",
        "version",
        "--format",
        "{{ .Server.Version }}",
    ]
    stdout, _, _ = await check_call(args)
    return stdout[0]


async def builder_prune() -> None:
    log.info("Purging Docker build cache")
    args = [
        "docker",
        "builder",
        "prune",
    ]
    await check_call(args)


EASY_MODE_DOCKERFILE = """
FROM {image}
WORKDIR /project
COPY . /project
RUN --mount=type=secret,id=.env env $(cat /run/secrets/.env | xargs) {command}
"""


def easy_mode_dockerfile(service: DiscoService) -> str:
    return EASY_MODE_DOCKERFILE.format(image=service.image, command=service.build)
