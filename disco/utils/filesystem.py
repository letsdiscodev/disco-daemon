import os


def projects_root() -> str:
    return "/code/projects"


def project_path(project_id: str) -> str:
    return f"/code/projects/{project_id}"


def project_folder_exists(project_id: str):
    return os.path.isdir(project_path(project_id))


def read_disco_file(project_id: str) -> str | None:
    path = f"{project_path(project_id)}/disco.json"
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
