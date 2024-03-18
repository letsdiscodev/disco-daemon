import logging
import subprocess
from datetime import datetime, timedelta
from multiprocessing import cpu_count
from typing import Callable

from disco.utils.discofile import DiscoFile
from disco.utils.filesystem import project_path

log = logging.getLogger(__name__)


def build_image(
    image: str,
    project_name: str,
    dockerfile: str,
    context: str,
    log_output: Callable[[str], None],
) -> None:
    args = [
        "docker",
        "build",
        "--cpu-period",
        "100000",  # default
        "--cpu-quota",
        # use half of the CPU time
        str(int(100000 * cpu_count() / 2)),
        "--no-cache",
        "--tag",
        image,
        "--file",
        dockerfile,
        context,
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=project_path(project_name),
    )
    assert process.stdout is not None
    for line in process.stdout:
        log_output(line.decode("utf-8"))

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def start_service(
    image: str,
    name: str,
    project_name: str,
    project_service_name: str,
    deployment_number: int,
    env_variables: list[tuple[str, str]],
    volumes: list[tuple[str, str]],
    published_ports: list[tuple[int, int, str]],
    networks: list[str],
    command: str | None,
    log_output: Callable[[str], None],
) -> None:
    more_args = []
    for var_name, var_value in env_variables:
        more_args.append("--env")
        more_args.append(f"{var_name}={var_value}")
    for volume, destination in volumes:
        more_args.append("--mount")
        more_args.append(
            f"type=volume,source=disco-volume-{volume},destination={destination}"
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
    for network in networks:
        more_args.append("--network")
        more_args.append(f"name={network},alias={project_service_name}")
    args = [
        "docker",
        "service",
        "create",
        "--name",
        name,
        "--with-registry-auth",
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
    timeout_seconds = 30
    timeout = datetime.utcnow() + timedelta(seconds=timeout_seconds)
    for line in process.stdout:
        log_output(line.decode("utf-8"))
        if datetime.utcnow() > timeout:
            process.terminate()
            raise Exception(
                f"Starting task failed, " f"timeout after {timeout_seconds} seconds"
            )

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def push_image(image: str, log_output: Callable[[str], None]) -> None:
    log_output(f"Pushing image {image}\n")
    args = [
        "docker",
        "push",
        image,
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    for line in process.stdout:
        log_output(line.decode("utf-8"))

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def stop_service(name: str, log_output: Callable[[str], None]) -> None:
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
        log_output(line.decode("utf-8"))

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def get_log_for_service(service_name: str) -> str:
    args = [
        "docker",
        "service",
        "logs",
        service_name,
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
    return output


def list_services_for_project(project_name: str) -> list[str]:
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


def list_containers_for_project(project_name: str) -> list[str]:
    args = [
        "docker",
        "container",
        "ls",
        "-a",
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
    containers = [line.decode("utf-8")[:-1] for line in process.stdout.readlines()]
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")
    return containers


def list_services_for_deployment(
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


def list_networks_for_project(project_name: str) -> list[str]:
    args = [
        "docker",
        "network",
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
    networks = [line.decode("utf-8")[:-1] for line in process.stdout.readlines()]
    process.wait()
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


def set_syslog_service(disco_host: str, syslog_urls: list[str]) -> None:
    # TODO we may want to just update the existing service instead
    #      to avoid losing logs while the new service is starting?
    args = [
        "docker",
        "service",
        "rm",
        "--name",
        "disco-syslog",
    ]
    try:
        log.info("Trying to stop existing syslog service")
        subprocess.run(
            args=args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        log.info("Stopped existing syslog service")
    except subprocess.CalledProcessError:
        log.info(
            "Failed to stop existing service. Expected if no syslog service was running"
        )
    if len(syslog_urls) == 0:
        log.info("No syslog URL specified, not starting new syslog service")
        return
    log.info("Starting new syslog service")
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
    try:
        subprocess.run(
            args=args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        log.info("New syslog service started")
    except subprocess.CalledProcessError as ex:
        raise Exception(ex.stdout.decode("utf-8")) from ex


def create_network(
    name: str, log_output: Callable[[str], None], project_name: str | None = None
) -> None:
    log_output(f"Creating network {name}\n")
    more_args = []
    if project_name is not None:
        more_args += [
            "--label",
            f"disco.project.name={project_name}",
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
        log_output(line.decode("utf-8"))

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def pull(image: str, log_output: Callable[[str], None]) -> None:
    log_output(f"Pulling Docker image {image}\n")
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
        log_output(line.decode("utf-8"))

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def remove_network(name: str, log_output: Callable[[str], None]) -> None:
    log_output(f"Removing network {name}\n")
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
        log_output(line.decode("utf-8"))

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def add_network_to_service(
    service: str, network: str, log_output: Callable[[str], None]
) -> None:
    log_output(f"Adding network {network} to service {service}\n")
    args = [
        "docker",
        "service",
        "update",
        "--network-add",
        network,
        service,
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    for line in process.stdout:
        log_output(line.decode("utf-8"))

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def remove_network_from_service(
    service: str, network: str, log_output: Callable[[str], None]
) -> None:
    log_output(f"Removing network {network} from service {service}\n")
    args = [
        "docker",
        "service",
        "update",
        "--network-rm",
        network,
        service,
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    for line in process.stdout:
        log_output(line.decode("utf-8"))

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def add_network_to_container(
    container: str, network: str, log_output: Callable[[str], None]
) -> None:
    log_output(f"Adding network {network} to container {container}\n")
    args = [
        "docker",
        "network",
        "connect",
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
        log_output(line.decode("utf-8"))

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def remove_network_from_container(
    container: str, network: str, log_output: Callable[[str], None]
) -> None:
    log_output(f"Removing network {network} from container {container}\n")
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
        log_output(line.decode("utf-8"))

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def run(
    image: str,
    project_name: str,
    name: str,
    env_variables: list[tuple[str, str]],
    volumes: list[tuple[str, str]],
    networks: list[str],
    command: str | None,
    log_output: Callable[[str], None],
    timeout: int = 600,
) -> None:
    try:
        more_args = []
        for var_name, var_value in env_variables:
            more_args.append("--env")
            more_args.append(f"{var_name}={var_value}")
        for volume, destination in volumes:
            more_args.append("--mount")
            more_args.append(
                f"type=volume,source=disco-volume-{volume},destination={destination}"
            )
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
        timeout_dt = datetime.utcnow() + timedelta(seconds=timeout)
        for line in process.stdout:
            log_output(line.decode("utf-8"))
            if datetime.utcnow() > timeout_dt:
                process.terminate()
                raise Exception(
                    f"Running command failed, timeout after {timeout} seconds"
                )

        process.wait()
        if process.returncode != 0:
            raise Exception(f"Docker returned status {process.returncode}")
        for network in networks:
            add_network_to_container(
                container=name, network=network, log_output=log_output
            )
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
        timeout_dt = datetime.utcnow() + timedelta(seconds=timeout)
        for line in process.stdout:
            log_output(line.decode("utf-8"))
            if datetime.utcnow() > timeout_dt:
                process.terminate()
                raise Exception(
                    f"Running command failed, timeout after {timeout} seconds"
                )

        process.wait()
        if process.returncode != 0:
            raise Exception(f"Docker returned status {process.returncode}")
    finally:
        remove_container(name, log_output)


def remove_container(name: str, log_output: Callable[[str], None]) -> None:
    log_output(f"Removing container {name}\n")
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
        log_output(line.decode("utf-8"))

    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def deployment_network_name(project_name: str, deployment_number: int) -> str:
    return f"disco-project-{project_name}-{deployment_number}"


def deployment_web_network_name(project_name: str, deployment_number: int) -> str:
    return f"disco-project-{project_name}-{deployment_number}-caddy"


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
    if service.image in disco_file.images:
        # image built by Disco
        return internal_image_name(
            registry_host=registry_host,
            project_name=project_name,
            deployment_number=deployment_number,
            image_name=service.image,
        )
    else:
        # image hosted in a Docker registry
        return service.image
