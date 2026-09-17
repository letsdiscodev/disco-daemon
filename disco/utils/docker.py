import asyncio
import json
import logging
import os
import re
import shlex
import signal
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from multiprocessing import cpu_count
from typing import AsyncGenerator, Awaitable, Callable, Literal

import disco
from disco.config import BUSYBOX_VERSION
from disco.errors import ProcessStatusError
from disco.utils import vectorconfig
from disco.utils.discofile import DiscoFile
from disco.utils.discofile import Service as DiscoService
from disco.utils.filesystem import project_path
from disco.utils.subprocess import call, check_call, decode_text

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
    env_variables += [
        ("DOT_ENV", dot_env),
        # suppress warning at end of build
        # WARNING: current commit information was not captured by the build:
        # git was not found in the system: exec: "git":
        # executable file not found in $PATH
        # https://github.com/docker/buildx/issues/1881
        ("BUILDX_GIT_INFO", "0"),
    ]
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
    health_command: str | None,
    extra_params: list[str],
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
    if health_command is not None:
        more_args.append("--health-cmd")
        more_args.append(health_command)
        more_args.append("--health-start-interval=3s")
        more_args.append("--health-start-period=300s")
    more_args.extend(extra_params)
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
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=20m",
        "--log-opt",
        "max-file=5",
        *more_args,
        image,
        *(shlex.split(command) if command is not None else []),
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


async def container_exists(container_name: str) -> bool:
    log.info("Checking if Docker container exists: %s", container_name)
    args = [
        "docker",
        "container",
        "inspect",
        container_name,
    ]
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await process.wait()
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
    log.info("Listing Docker services for project %s", project_name)
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


async def list_project_networks() -> list[str]:
    log.info("Listing all project networks")
    args = [
        "docker",
        "network",
        "ls",
        "--filter",
        "label=disco.project.name",
        "--format",
        "{{ .Name }}",
    ]
    stdout, _, _ = await check_call(args)
    return stdout


async def inspect_network(name: str) -> dict:
    log.info("Inspecting network %s", name)
    args = [
        "docker",
        "network",
        "inspect",
        "--verbose",
        name,
    ]
    stdout, _, _ = await check_call(args)
    json_str = "\n".join(stdout)
    parsed_json = json.loads(json_str)
    return parsed_json[0]


async def remove_network(name: str) -> None:
    log.info("Removing network %s", name)
    args = [
        "docker",
        "network",
        "rm",
        name,
    ]
    await check_call(args)


def internal_image_name(
    registry: str | None,
    project_name: str,
    deployment_number: int,
    image_name: str,
) -> str:
    base_name = f"disco/project-{project_name}-{image_name}:{deployment_number}"
    if registry is None:
        return base_name
    return f"{registry}/{base_name}"


def service_name(project_name: str, service: str, deployment_number: int) -> str:
    return f"{project_name}-{deployment_number}-{service}"


@dataclass
class SyslogService:
    name: str
    type: str
    url: str
    # None on a logspout service (before 0.34.0)
    impl: str | None = None
    image: str | None = None
    config: str | None = None


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
        labels = service_data["Spec"]["Labels"]
        service = SyslogService(
            name=service_data["Spec"]["Name"],
            type=labels["disco.syslog.type"],
            url=labels["disco.syslog.url"],
            impl=labels.get("disco.syslog.impl"),
            image=labels.get("disco.syslog.image"),
            config=labels.get("disco.syslog.config"),
        )
        services.append(service)
    return services


SYSLOG_CONFIG_PREFIX = "disco-syslog-cfg-"
STREAM_CONFIG_PREFIX = "disco-stream-cfg-"
SYSLOG_BUFFER_VOLUME_PREFIX = "disco-vector-buffer-"
# hard caps for one collector task per node (observed under a 50k lines/s burst on
# 0.58.0: ~140 MB udp, ~80 MB tls); the disk buffer survives an oom restart
SYSLOG_TASK_MEMORY_LIMIT = "512m"
STREAM_TASK_MEMORY_LIMIT = "256m"


def syslog_service_name(url: str, type: Literal["CORE", "GLOBAL"], config: str) -> str:
    """deterministic: the same destination with the same rendered config has one name,
    a config change gets a new name so the new service can overlap the old one."""
    dest = vectorconfig.destination_id(url, type)
    return f"disco-syslog-{dest}-{vectorconfig.config_hash(config)}"


def syslog_config_name(config: str) -> str:
    return f"{SYSLOG_CONFIG_PREFIX}{vectorconfig.config_hash(config)}"


def stream_config_name(config: str) -> str:
    return f"{STREAM_CONFIG_PREFIX}{vectorconfig.config_hash(config)}"


def syslog_buffer_volume_name(url: str, type: Literal["CORE", "GLOBAL"]) -> str:
    return f"{SYSLOG_BUFFER_VOLUME_PREFIX}{vectorconfig.destination_id(url, type)}"


def build_syslog_service_args(
    disco_host: str,
    url: str,
    type: Literal["CORE", "GLOBAL"],
    config: str,
) -> list[str]:
    """pure: the `docker service create` argv for one vector syslog collector."""
    config_name = syslog_config_name(config)
    return [
        "docker",
        "service",
        "create",
        "--name",
        syslog_service_name(url, type, config),
        "--detach",
        "--label",
        "disco.syslog",
        "--label",
        f"disco.syslog.url={url}",
        "--label",
        f"disco.syslog.type={type}",
        "--label",
        "disco.syslog.impl=vector",
        "--label",
        f"disco.syslog.image={vectorconfig.VECTOR_IMAGE}",
        "--label",
        f"disco.syslog.config={config_name}",
        "--config",
        f"source={config_name},target={vectorconfig.VECTOR_CONFIG_PATH}",
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
        "--mount",
        f"type=volume,source={syslog_buffer_volume_name(url, type)},"
        f"target={vectorconfig.VECTOR_DATA_DIR}",
        "--env",
        f"{vectorconfig.HOSTNAME_ENV}={disco_host}",
        "--mode",
        "global",
        "--limit-memory",
        SYSLOG_TASK_MEMORY_LIMIT,
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=20m",
        "--log-opt",
        "max-file=5",
        vectorconfig.VECTOR_IMAGE,
        "--config",
        vectorconfig.VECTOR_CONFIG_PATH,
    ]


async def config_exists(name: str) -> bool:
    process = await asyncio.create_subprocess_exec(
        "docker",
        "config",
        "inspect",
        name,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    await process.wait()
    return process.returncode == 0


async def create_config(name: str, content: str) -> None:
    """swarm configs are immutable: creating an existing name is a no-op here."""
    if await config_exists(name):
        return
    log.info("Creating Docker config %s", name)
    await check_call(["docker", "config", "create", name, "-"], stdin=content)


async def rm_config(name: str) -> None:
    log.info("Removing Docker config %s", name)
    await check_call(["docker", "config", "rm", name])


async def list_configs(prefix: str) -> list[str]:
    stdout, _, _ = await check_call(
        ["docker", "config", "ls", "--format", "{{ .Name }}"]
    )
    return [name for name in stdout if name.startswith(prefix)]


async def services_using_config(config_name: str) -> list[str]:
    stdout, _, _ = await check_call(
        [
            "docker",
            "service",
            "ls",
            "--filter",
            f"label=disco.syslog.config={config_name}",
            "--format",
            "{{ .Name }}",
        ]
    )
    return stdout


async def rm_config_if_unused(config_name: str) -> None:
    if len(await services_using_config(config_name)) > 0:
        return
    if await config_exists(config_name):
        await rm_config(config_name)


async def prune_logging_configs() -> None:
    """configs of collectors that no longer exist (a crash between rm and cleanup)."""
    for prefix in (SYSLOG_CONFIG_PREFIX, STREAM_CONFIG_PREFIX):
        for name in await list_configs(prefix):
            await rm_config_if_unused(name)


async def start_syslog_service(
    disco_host: str,
    url: str,
    type: Literal["CORE", "GLOBAL"],
    buffer_bytes: int = vectorconfig.DEFAULT_DESTINATION_BUFFER_BYTES,
) -> str:
    """create the vector collector for one destination; returns the service name."""
    config = vectorconfig.render_syslog_config(url, type, buffer_bytes)
    name = syslog_service_name(url, type, config)
    log.info("Starting Syslog service %s for %s %s", name, url, type)
    # the rendered config is logged so it exists somewhere other than the swarm store
    log.info("Vector config for %s %s:\n%s", url, type, config)
    await create_config(syslog_config_name(config), config)
    await check_call(build_syslog_service_args(disco_host, url, type, config))
    return name


async def rm_syslog_service(service: SyslogService) -> None:
    log.info(
        "Stopping Syslog service %s (%s %s)", service.name, service.url, service.type
    )
    await rm_service(service.name)
    if service.config is not None:
        await rm_config_if_unused(service.config)
    if service.impl == "vector":
        # the buffer volume on this node; on worker nodes the local volume stays until
        # the node is pruned (a global service leaves one per node)
        volume = syslog_buffer_volume_name(service.url, service.type)  # type: ignore[arg-type]
        await call(["docker", "volume", "rm", volume])


@dataclass
class LabelledService:
    name: str
    labels: dict[str, str]


async def get_service_labels(service_name: str) -> dict[str, str]:
    stdout, _, _ = await check_call(
        [
            "docker",
            "service",
            "inspect",
            "--format",
            "{{ json .Spec.Labels }}",
            service_name,
        ]
    )
    labels = json.loads("\n".join(stdout) or "{}")
    return labels or {}


async def list_project_services_with_labels(
    project_name: str | None,
) -> list[LabelledService]:
    """every project service (label disco.project.name), or those of one project."""
    label = (
        "disco.project.name"
        if project_name is None
        else f"disco.project.name={project_name}"
    )
    stdout, _, _ = await check_call(
        [
            "docker",
            "service",
            "ls",
            "--filter",
            f"label={label}",
            "--format",
            "{{ .Name }}",
        ]
    )
    services = []
    for name in stdout:
        services.append(
            LabelledService(name=name, labels=await get_service_labels(name))
        )
    return services


async def list_streaming_services() -> list[str]:
    stdout, _, _ = await check_call(
        [
            "docker",
            "service",
            "ls",
            "--filter",
            "label=disco.syslogs",
            "--format",
            "{{ .Name }}",
        ]
    )
    return stdout


async def running_task_nodes(service_name: str) -> set[str]:
    """node ids with a running task of the service."""
    stdout, _, _ = await check_call(
        [
            "docker",
            "service",
            "ps",
            service_name,
            "--filter",
            "desired-state=running",
            "--format",
            "{{ .Node }} {{ .CurrentState }}",
            "--no-trunc",
        ]
    )
    return {
        line.split(" ", 1)[0]
        for line in stdout
        if line.split(" ", 1)[1].startswith("Running")
    }


async def eligible_nodes() -> set[str]:
    """nodes a global service gets a task on: ready and active."""
    stdout, _, _ = await check_call(
        [
            "docker",
            "node",
            "ls",
            "--format",
            "{{ .Hostname }} {{ .Status }} {{ .Availability }}",
        ]
    )
    return {
        line.split()[0] for line in stdout if line.split()[1:] == ["Ready", "Active"]
    }


async def wait_for_global_service(service_name: str, timeout: float = 180) -> bool:
    """true once the service has a running task on every eligible node."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        nodes = await eligible_nodes()
        running = await running_task_nodes(service_name)
        if len(nodes) > 0 and nodes <= running:
            return True
        if asyncio.get_running_loop().time() > deadline:
            log.warning(
                "Service %s not running everywhere after %ss: nodes=%s running=%s",
                service_name,
                timeout,
                sorted(nodes),
                sorted(running),
            )
            return False
        await asyncio.sleep(2)


async def update_syslog_hostname(service_name: str, disco_host: str) -> None:
    args = [
        "docker",
        "service",
        "update",
        service_name,
        "--env-add",
        f"SYSLOG_HOSTNAME={disco_host}",
        "--detach",
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
        disco.daemon_image(),
        "docker",
        "run",
        "--rm",
        "--detach",
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
        disco.daemon_image(),
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


async def add_network_to_container(
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
            "--log-driver",
            "json-file",
            "--log-opt",
            "max-size=20m",
            "--log-opt",
            "max-file=5",
            *more_args,
            image,
            *(shlex.split(command) if command is not None else []),
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
    registry: str | None,
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
            registry=registry,
            project_name=project_name,
            deployment_number=deployment_number,
            image_name=service_name,
        )
    if service.image in disco_file.images:
        # image defined in Discofile
        return internal_image_name(
            registry=registry,
            project_name=project_name,
            deployment_number=deployment_number,
            image_name=service.image,
        )
    else:
        # image hosted in a Docker registry
        return service.image


async def login(
    disco_host_home: str, address: str, username: str, password: str
) -> None:
    log.info("Docker login to %s", address)
    args = [
        "docker",
        "run",
        "--rm",
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
        "--mount",
        f"type=bind,source={disco_host_home},target=/root",
        "--interactive",
        disco.daemon_image(),
        "docker",
        "login",
        "--username",
        username,
        "--password-stdin",
        address,
    ]
    await check_call(args, stdin=password)


async def logout(disco_host_home: str, address: str) -> None:
    log.info("Docker logout from %s", address)
    args = [
        "docker",
        "run",
        "--rm",
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
        "--mount",
        f"type=bind,source={disco_host_home},target=/root",
        disco.daemon_image(),
        "docker",
        "logout",
        address,
    ]
    await check_call(args)


async def get_authenticated_registries(disco_host_home: str) -> list[str]:
    log.info("Getting list of authenticated Docker registries")
    args = [
        "docker",
        "run",
        "--rm",
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
        "--mount",
        f"type=bind,source={disco_host_home},target=/root",
        disco.daemon_image(),
        "cat",
        f"{disco_host_home}/.docker/config.json",
    ]
    stdout, _, process = await call(args)
    if process.returncode == 0:
        config_json_str = "\n".join(stdout)
        config = json.loads(config_json_str)
        return list(config.get("auths", {}).keys())
    else:
        return []


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
            disco.daemon_image(),
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
            disco.daemon_image(),
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
        "--force",  # do not prompt for confirmation
    ]
    await check_call(args)


async def get_caddy_container() -> str:
    """The container of the disco-caddy service task on this node."""
    args = [
        "docker",
        "ps",
        "--quiet",
        "--filter",
        "label=com.docker.swarm.service.name=disco-caddy",
    ]
    stdout, _, process = await call(args)
    if process.returncode != 0 or len(stdout) == 0:
        raise RuntimeError("Caddy container not found")
    return stdout[0].strip()


async def caddy_nc(service_name: str, port: int) -> bool:
    """Run netcat in Caddy's container."""
    log.info("Running nc in Caddy's container for %s:%d", service_name, port)
    container = await get_caddy_container()
    args = [
        "docker",
        "exec",
        container,
        "nc",
        "-zv",
        service_name,
        str(port),
    ]
    _, _, process = await call(args)
    return process.returncode == 0


@dataclass
class DiskFree:
    used: int
    available: int


async def host_df() -> DiskFree:
    log.info("Getting host disk usage (df)")
    args = [
        "docker",
        "run",
        "--rm",
        "-v",
        "/:/hostroot:ro",
        f"busybox:{BUSYBOX_VERSION}",
        "df",
        "/hostroot",
    ]
    stdout, _, _ = await check_call(args)
    # Filesystem           1K-blocks      Used Available Use% Mounted on
    # /dev/sda1            235972036  32875864 193451672  15% /hostroot
    _, _, used, available, _, _ = stdout[1].split()
    return DiskFree(
        used=int(used),
        available=int(available),
    )


EASY_MODE_DOCKERFILE = """
FROM {image}
WORKDIR /project
COPY . /project
RUN --mount=type=secret,id=.env env $(cat /run/secrets/.env | xargs) {command}
"""


def easy_mode_dockerfile(service: DiscoService) -> str:
    return EASY_MODE_DOCKERFILE.format(image=service.image, command=service.build)
