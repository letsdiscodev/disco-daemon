import logging
import os
import shutil

log = logging.getLogger(__name__)


def projects_root() -> str:
    return "/disco/projects"


def project_path(project_name: str) -> str:
    return f"/disco/projects/{project_name}"


def project_folder_exists(project_name: str):
    return os.path.isdir(project_path(project_name))


def read_disco_file(project_name: str) -> str | None:
    path = f"{project_path(project_name)}/disco.json"
    log.info("Reading disco file %s", path)
    if not os.path.isfile(path):
        log.info("Disco file does not exist, not reading %s", path)
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def static_sites_root() -> str:
    return "/disco/srv"


def static_site_deployment_path(project_name: str, deployment_number: int) -> str:
    return f"/disco/srv/{project_name}/{deployment_number}"


def static_site_src_path(project_name: str, public_path: str) -> str:
    path = os.path.abspath(f"{project_path(project_name)}{public_path}")
    if not path.startswith(f"{project_path(project_name)}/"):
        # prevent traversal attacks
        raise Exception("publicPath must be inside project folder")
    return path


def copy_static_site_src_to_deployment_folder(
    project_name: str, public_path: str, deployment_number: int
) -> None:
    src_path = static_site_src_path(project_name, public_path)
    dst_path = static_site_deployment_path(project_name, deployment_number)
    shutil.copytree(src_path, dst_path)
