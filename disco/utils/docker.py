import hashlib
import logging
import subprocess
from typing import Callable

from disco.models import ApiKey
from disco.utils.filesystem import project_path

log = logging.getLogger(__name__)


def build_image(
    image: str,
    project_id: str,
    dockerfile: str,
    context: str,
    log_output: Callable[[str], None],
) -> None:
    args = [
        "docker",
        "build",
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
        cwd=project_path(project_id),
    )
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
    env_variables: list[tuple[str, str]],
    volumes: list[tuple[str, str]],
    published_ports: list[tuple[int, int, str]],
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
    args = [
        "docker",
        "service",
        "create",
        "--name",
        name,
        "--network=disco-network",
        "--with-registry-auth",
        "--label",
        f"disco.project.name={project_name}",
        "--label",
        f"disco.service.name={project_service_name}",
        "--container-label",
        f"disco.project.name={project_name}",
        "--container-label",
        f"disco.service.name={project_service_name}",
        *more_args,
        image,
        *(command.split() if command is not None else []),
    ]
    process = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    for line in process.stdout:
        log_output(line.decode("utf-8"))
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def push_image(image: str, log_output: Callable[[str], None]) -> None:
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
    for line in process.stdout:
        log_output(line.decode("utf-8"))
    process.wait()
    if process.returncode != 0:
        raise Exception(f"Docker returned status {process.returncode}")


def image_name(
    disco_domain: str,
    project_id: str,
    deployment_number: int,
    dockerfile: str,
    context: str,
) -> str:
    h = hashlib.new("sha256")
    h.update(f"dockerfile={dockerfile}&context={context}".encode("utf-8"))
    config_hash = h.hexdigest()
    return (
        f"{disco_domain}/disco/project-{project_id}-{config_hash}:{deployment_number}"
    )


def service_name(project_name: str, service: str, deployment_number: int) -> str:
    return f"{project_name}-{deployment_number}-{service}"


def get_all_volumes() -> list[str]:
    args = [
        "docker",
        "volume",
        "ls",
    ]
    try:
        result = subprocess.run(
            args=args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        lines = result.stdout.decode("utf-8").split("\n")
        lines.pop(0)  # headers
        volumes = []
        for line in lines:
            if len(line) == 0:
                continue
            name = line.split(" ")[-1]
            if name.startswith("disco-volume-"):
                volumes.append(name[13:])
        return volumes
    except subprocess.CalledProcessError as ex:
        raise Exception(ex.stdout.decode("utf-8")) from ex


def create_volume(name: str, by_api_key: ApiKey) -> None:
    log.info("Creating volume %s by %s", name, by_api_key.log())
    args = [
        "docker",
        "volume",
        "create",
        f"disco-volume-{name}",
    ]
    try:
        subprocess.run(
            args=args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as ex:
        raise Exception(ex.stdout.decode("utf-8")) from ex


def delete_volume(name: str, by_api_key: ApiKey) -> None:
    log.info("Deleting volume %s by %s", name, by_api_key.log())
    args = [
        "docker",
        "volume",
        "rm",
        f"disco-volume-{name}",
    ]
    try:
        subprocess.run(
            args=args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as ex:
        raise Exception(ex.stdout.decode("utf-8")) from ex


def set_syslog_service(disco_domain: str, syslog_urls: list[str]) -> None:
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
        f"SYSLOG_HOSTNAME={disco_domain}",
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
