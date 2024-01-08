import os
import subprocess

from disco.models import Project


def pull(project: Project) -> None:
    args = ["git", "pull"]
    directory = f"/code/projects/{project.name}"
    if not os.path.isdir(directory):
        _clone_project(project)
    else:
        try:
            subprocess.run(
                args=args,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=f"/code/projects/{project.name}",
            )
        except subprocess.CalledProcessError as ex:
            raise Exception(ex.stdout.decode("utf-8")) from ex


def _clone_project(project: Project) -> None:
    url = project.github_repo.replace("github.com", project.github_host)
    args = ["git", "clone", url, f"/code/projects/{project.name}"]
    try:
        subprocess.run(
            args=args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as ex:
        raise Exception(ex.stdout.decode("utf-8")) from ex
