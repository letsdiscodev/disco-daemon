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
        _image_name(project_name, build_number),
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
    project_name: str, build_number: int, env_variables: list[tuple[str, str]]
) -> None:
    env_var_args = []
    for var_name, var_value in env_variables:
        env_var_args.append("-e")
        env_var_args.append(f"{var_name}={var_value}")
    args = [
        "docker",
        "run",
        "--name",
        _container_name(project_name, build_number),
        "-d",
        "--restart",
        "unless-stopped",
        "--expose",
        "8000",
        "--network=disco-network",
        *env_var_args,
        _image_name(project_name, build_number),
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
        _image_name(project_name, build_number - 1),
        _image_name(project_name, build_number),
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
        _container_name(project_name, build_number),
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
        _container_name(project_name, build_number),
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


def _image_name(project_name: str, build_number: int) -> str:
    return f"disco/project-{project_name}:{build_number}"


def _container_name(project_name: str, build_number: int) -> str:
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
