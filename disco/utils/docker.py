import logging
import subprocess

from disco.models import ApiKey

log = logging.getLogger(__name__)


def build_project(project_name: str, build_number: int) -> None:
    args = [
        "docker",
        "build",
        "--no-cache",
        "-t",
        image_name(project_name, build_number),
        f"/code/projects/{project_name}/.",
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


def start_container(
    image: str,
    container: str,
    env_variables: list[tuple[str, str]],
    volumes: list[tuple[str, str]],
    published_ports: list[tuple[int, int]],
    exposed_ports: list[int],
) -> None:
    env_var_args = []
    volume_args = []
    port_args = []
    exposed_ports = []
    for var_name, var_value in env_variables:
        env_var_args.append("-e")
        env_var_args.append(f"{var_name}={var_value}")
    for volume, destination in volumes:
        volume_args.append("--mount")
        volume_args.append(
            f"type=volume,source=disco-volume-{volume},destination={destination}"
        )
    for host_port, container_port in published_ports:
        port_args.append("--publish")
        port_args.append(f"{host_port}:{container_port}")
    for port in exposed_ports:
        port_args.append("--expose")
        port_args.append(str(port))
    args = [
        "docker",
        "run",
        "--name",
        container,
        "-d",
        "--restart",
        "unless-stopped",
        "--network=disco-network",
        *env_var_args,
        *volume_args,
        *port_args,
        image,
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


def tag_previous_image_as_current(project_name: str, build_number: int) -> None:
    args = [
        "docker",
        "tag",
        image_name(project_name, build_number - 1),
        image_name(project_name, build_number),
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


def stop_container(project_name: str, build_number: int) -> None:
    args = [
        "docker",
        "stop",
        container_name(project_name, build_number),
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


def remove_container(project_name: str, build_number: int) -> None:
    args = [
        "docker",
        "rm",
        container_name(project_name, build_number),
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


def image_name(project_name: str, build_number: int) -> str:
    return f"disco/project-{project_name}:{build_number}"


def container_name(project_name: str, build_number: int) -> str:
    return f"disco-project-{project_name}-{build_number}"


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


def image_exists(image: str) -> bool:
    args = [
        "docker",
        "image",
        "inspect",
        image,
    ]
    try:
        subprocess.run(
            args=args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def pull_image(image: str) -> None:
    args = [
        "docker",
        "pull",
        image,
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
