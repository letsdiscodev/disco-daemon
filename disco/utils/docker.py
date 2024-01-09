import subprocess


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


def start_container(project_name: str, build_number: int) -> None:
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
