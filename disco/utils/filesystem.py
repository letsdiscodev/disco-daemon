import logging
import os

log = logging.getLogger(__name__)


def projects_root() -> str:
    return "/code/projects"


def project_path(project_name: str) -> str:
    return f"/code/projects/{project_name}"


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
